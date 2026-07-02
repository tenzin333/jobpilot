"""Phase 3 acceptance: hard filters, schema hardening, rank orchestration."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

from app.config import Preferences, RemotePreference  # noqa: E402
from app.llm import ranking  # noqa: E402
from app.llm.client import json_schema_format  # noqa: E402
from app.llm.ranking import (  # noqa: E402
    MatchScore,
    PrefilterScore,
    passes_hard_filters,
    passes_location,
    passes_salary,
    passes_sponsorship,
    rank_jobs,
)
from app.models import Application, Job, Profile  # noqa: E402


def _job(**kw) -> Job:
    base = dict(source="greenhouse", source_job_id="x", company="Acme", title="Engineer",
                location="", remote=False, dedup_hash="h", ats_type="greenhouse", description="")
    base.update(kw)
    return Job(**base)


def test_location_filter():
    remote_only = Preferences(remote_preference=RemotePreference.remote_only, locations=["New York"])
    assert passes_location(_job(remote=True), remote_only)
    assert not passes_location(_job(remote=False, location="New York"), remote_only)

    hybrid = Preferences(remote_preference=RemotePreference.hybrid_ok, locations=["New York"])
    assert passes_location(_job(location="New York, NY"), hybrid)
    assert not passes_location(_job(location="Austin, TX"), hybrid)
    assert passes_location(_job(remote=True), hybrid)


def test_salary_filter():
    prefs = Preferences(min_salary=150000)
    assert passes_salary(_job(salary_max=None), prefs)  # unknown -> keep
    assert passes_salary(_job(salary_max=160000), prefs)
    assert not passes_salary(_job(salary_max=120000), prefs)


def test_sponsorship_filter():
    prefs = Preferences(require_sponsorship=True)
    assert passes_sponsorship(_job(description="Great role. Visa sponsorship available."), prefs)
    assert not passes_sponsorship(_job(description="We do not provide visa sponsorship."), prefs)
    # If not required, sponsorship language is irrelevant.
    assert passes_sponsorship(_job(description="No visa sponsorship."), Preferences(require_sponsorship=False))


def test_hard_filters_combined():
    prefs = Preferences(remote_preference=RemotePreference.remote_only, min_salary=100000)
    assert passes_hard_filters(_job(remote=True, salary_max=120000), prefs)
    assert not passes_hard_filters(_job(remote=False, salary_max=120000), prefs)


def test_json_schema_format_is_strict():
    fmt = json_schema_format(PrefilterScore)
    assert fmt["type"] == "json_schema"
    schema = fmt["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"score", "reason"}


def test_rank_jobs_orchestration(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    profile = Profile(full_name="Test", raw_text="Engineer", skills=["python"])
    session.add(profile)
    # Two jobs pass hard filters (remote), one fails (onsite, wrong location).
    j1 = _job(title="Backend Engineer", remote=True, dedup_hash="a", source_job_id="1")
    j2 = _job(title="Platform Engineer", remote=True, dedup_hash="b", source_job_id="2")
    j3 = _job(title="Sales", remote=False, location="Tokyo", dedup_hash="c", source_job_id="3")
    session.add_all([j1, j2, j3])
    session.commit()

    prefs = Preferences(remote_preference=RemotePreference.remote_only)

    # Stub the LLM stages.
    monkeypatch.setattr(
        ranking, "prefilter",
        lambda jobs, profile: {j.id: PrefilterScore(score=8, reason="ok") for j in jobs},
    )
    monkeypatch.setattr(
        ranking, "deep_score",
        lambda job, profile: MatchScore(score=85, rationale="strong", gaps=["k8s"]),
    )

    stats = rank_jobs(session, profile, prefs)
    assert stats["candidates"] == 2  # j3 hard-filtered out
    assert stats["ranked"] == 2

    apps = session.exec(select(Application)).all()
    assert len(apps) == 2
    assert all(a.match_score == 85 for a in apps)
    assert all(a.status == "ranked" for a in apps)

    # Re-running ranks nothing new (already have applications).
    again = rank_jobs(session, profile, prefs)
    assert again["ranked"] == 0
