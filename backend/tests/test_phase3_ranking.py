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


def test_location_any_with_only_remote_listed_keeps_onsite_jobs():
    """Regression: remote_preference='any' with locations=['Remote'] must NOT
    reject on-site jobs. 'Remote' in the list expresses remote interest, not a
    geographic restriction, so it must not make `any` behave like remote_only."""
    prefs = Preferences(remote_preference=RemotePreference.any, locations=["Remote"])
    assert passes_location(_job(remote=False, location="San Francisco"), prefs)
    assert passes_location(_job(remote=False, location="Fort Myers, FL"), prefs)
    assert passes_location(_job(remote=True), prefs)
    # But a named city still gates on-site jobs to that city.
    picky = Preferences(remote_preference=RemotePreference.any, locations=["Remote", "Boston"])
    assert passes_location(_job(remote=False, location="Boston, MA"), picky)
    assert not passes_location(_job(remote=False, location="Denver, CO"), picky)


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


_DESC = "We are hiring an engineer to build and operate backend services. " * 3


def _stub_llm(monkeypatch, deep_score=85):
    monkeypatch.setattr(
        ranking, "prefilter",
        lambda jobs, profile: {j.id: PrefilterScore(score=8, reason="ok") for j in jobs},
    )
    monkeypatch.setattr(
        ranking, "deep_score_batch",
        lambda jobs, profile: {j.id: MatchScore(score=deep_score, rationale="strong", gaps=["k8s"]) for j in jobs},
    )


def _mem_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_rank_jobs_orchestration(monkeypatch):
    session = _mem_session()
    profile = Profile(full_name="Test", raw_text="Engineer", skills=["python"])
    session.add(profile)
    # Two jobs pass hard filters (remote, with descriptions), one fails (onsite, wrong location).
    j1 = _job(title="Backend Engineer", remote=True, dedup_hash="a", source_job_id="1", description=_DESC)
    j2 = _job(title="Platform Engineer", remote=True, dedup_hash="b", source_job_id="2", description=_DESC)
    j3 = _job(title="Sales", remote=False, location="Tokyo", dedup_hash="c", source_job_id="3")
    session.add_all([j1, j2, j3])
    session.commit()

    prefs = Preferences(remote_preference=RemotePreference.remote_only)
    _stub_llm(monkeypatch)

    stats = rank_jobs(session, profile, prefs)
    assert stats["candidates"] == 2  # j3 hard-filtered out
    assert stats["ranked"] == 2

    apps = session.exec(select(Application)).all()
    assert len(apps) == 2
    assert all(a.match_score == 85 for a in apps)  # deep score applied (jobs have descriptions)
    assert all(a.status == "ranked" for a in apps)


def test_rank_jobs_rescore_recovers_rate_limited_job(monkeypatch):
    """A job whose deep score was rate-limited (kept the prefilter score, nothing
    cached) is refreshed on Re-score — updated in place, no duplicate app."""
    session = _mem_session()
    profile = Profile(full_name="Test", raw_text="Engineer", skills=["python"])
    session.add(profile)
    j1 = _job(title="Backend Engineer", remote=True, dedup_hash="a", source_job_id="1", description=_DESC)
    session.add(j1)
    session.commit()
    prefs = Preferences(remote_preference=RemotePreference.remote_only)

    monkeypatch.setattr(
        ranking, "prefilter",
        lambda jobs, profile: {j.id: PrefilterScore(score=8, reason="ok") for j in jobs},
    )
    # First run: deep scoring returns nothing (rate-limited) -> keeps prefilter 80.
    monkeypatch.setattr(ranking, "deep_score_batch", lambda jobs, profile: {})
    rank_jobs(session, profile, prefs)
    apps = session.exec(select(Application)).all()
    assert len(apps) == 1 and apps[0].match_score == 80

    # Re-score: deep scoring now succeeds -> same app updated to 88, not duplicated.
    monkeypatch.setattr(
        ranking, "deep_score_batch",
        lambda jobs, profile: {j.id: MatchScore(score=88, rationale="strong") for j in jobs},
    )
    again = rank_jobs(session, profile, prefs)
    assert again["ranked"] == 1
    apps = session.exec(select(Application)).all()
    assert len(apps) == 1 and apps[0].match_score == 88


def test_rank_jobs_caches_scores_across_runs(monkeypatch):
    """Re-running with identical inputs serves both stages from the score cache —
    no repeat LLM calls (so re-runs don't burn the free-tier rate limit)."""
    session = _mem_session()
    profile = Profile(full_name="Test", raw_text="Engineer", skills=["python"])
    session.add(profile)
    j1 = _job(title="Backend Engineer", remote=True, dedup_hash="a", source_job_id="1", description=_DESC)
    session.add(j1)
    session.commit()
    prefs = Preferences(remote_preference=RemotePreference.remote_only)

    calls = {"prefilter": 0, "deep": 0}

    def _pf(jobs, profile):
        calls["prefilter"] += 1
        return {j.id: PrefilterScore(score=8, reason="ok") for j in jobs}

    def _deep(jobs, profile):
        calls["deep"] += 1
        return {j.id: MatchScore(score=85, rationale="strong") for j in jobs}

    monkeypatch.setattr(ranking, "prefilter", _pf)
    monkeypatch.setattr(ranking, "deep_score_batch", _deep)

    rank_jobs(session, profile, prefs)
    assert calls == {"prefilter": 1, "deep": 1}

    # Second run, same inputs: both stages hit the cache -> no new LLM calls.
    rank_jobs(session, profile, prefs)
    assert calls == {"prefilter": 1, "deep": 1}

    # Editing the job's description changes its cache key -> deep recomputes.
    j1.description = _DESC + " Now with Kubernetes and Go."
    session.add(j1)
    session.commit()
    rank_jobs(session, profile, prefs)
    assert calls["deep"] == 2


def test_rank_jobs_skips_deep_score_for_descriptionless_jobs(monkeypatch):
    """A title-only job (e.g. LinkedIn guest card) keeps its prefilter score
    instead of being deep-scored to a noisy 0."""
    session = _mem_session()
    profile = Profile(full_name="Test", raw_text="Engineer", skills=["python"])
    session.add(profile)
    j1 = _job(title="AI Engineer", remote=True, dedup_hash="a", source_job_id="1", description="")
    session.add(j1)
    session.commit()
    prefs = Preferences(remote_preference=RemotePreference.remote_only)

    # deep_score_batch would return 0, but a description-less job must never reach it.
    called = {"deep": False}

    def _spy_deep(jobs, profile):
        called["deep"] = True
        return {j.id: MatchScore(score=0, rationale="no info") for j in jobs}

    monkeypatch.setattr(
        ranking, "prefilter",
        lambda jobs, profile: {j.id: PrefilterScore(score=6, reason="title match") for j in jobs},
    )
    monkeypatch.setattr(ranking, "deep_score_batch", _spy_deep)

    rank_jobs(session, profile, prefs)
    app = session.exec(select(Application)).one()
    assert called["deep"] is False           # skipped — nothing to deep-score
    assert app.match_score == 60             # prefilter 6/10 -> 60, not 0
    assert "no job description" in app.score_rationale.lower()


def test_rank_jobs_leaves_advanced_apps_untouched(monkeypatch):
    """A job whose application has advanced past 'ranked' (e.g. submitted) is not
    re-scored, so Re-score can't clobber in-flight/done work."""
    session = _mem_session()
    profile = Profile(full_name="Test", raw_text="Engineer", skills=["python"])
    session.add(profile)
    j1 = _job(title="Backend Engineer", remote=True, dedup_hash="a", source_job_id="1", description=_DESC)
    session.add(j1)
    session.commit()
    session.add(Application(job_id=j1.id, status="submitted", match_score=90))
    session.commit()
    prefs = Preferences(remote_preference=RemotePreference.remote_only)
    _stub_llm(monkeypatch, deep_score=10)

    stats = rank_jobs(session, profile, prefs)
    assert stats["candidates"] == 0          # locked by the submitted app
    app = session.exec(select(Application)).one()
    assert app.status == "submitted" and app.match_score == 90  # untouched
