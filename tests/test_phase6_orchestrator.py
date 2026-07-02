"""Phase 6 acceptance: full dry-run cycle, idempotency, kill switch."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

from app.config import Preferences, SourceConfig  # noqa: E402
from app.controls import update_control  # noqa: E402
from app.discovery.base import RawJob  # noqa: E402
from app.llm import ranking  # noqa: E402
from app.llm.ranking import MatchScore, PrefilterScore  # noqa: E402
from app.llm.tailoring import TailoredResume  # noqa: E402
from app.models import Application, ApplicationStatus, Job, Profile  # noqa: E402
from app.pipeline import ingest as ingest_mod  # noqa: E402
from app.pipeline import orchestrator as orch  # noqa: E402
from app.pipeline import submit as submit_pipeline  # noqa: E402
from app.pipeline import tailor as tailor_pipeline  # noqa: E402
from app.submit.base import DryRunFilled, Submitted


def _session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _raws() -> list[RawJob]:
    return [
        RawJob(source="greenhouse", source_job_id="1", company="Acme", title="Backend Engineer",
               location="Remote", remote=True, apply_url="https://x/1"),
        RawJob(source="greenhouse", source_job_id="2", company="Acme", title="Platform Engineer",
               location="Remote", remote=True, apply_url="https://x/2"),
    ]


class _FakeAdapter:
    name = "greenhouse"
    field_map = None

    def __init__(self, result):
        self._result = result

    def submit(self, **kwargs):
        return self._result


def _wire_stubs(monkeypatch, *, adapter_result):
    prefs = Preferences(sources={"greenhouse": SourceConfig(enabled=True, autonomy=True, companies=["acme"])})
    monkeypatch.setattr(orch, "get_preferences", lambda: prefs)

    async def fake_fetch_all(_prefs):
        return _raws()

    monkeypatch.setattr(ingest_mod, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(ranking, "prefilter", lambda jobs, profile: {j.id: PrefilterScore(score=9) for j in jobs})
    monkeypatch.setattr(ranking, "deep_score", lambda job, profile: MatchScore(score=88, rationale="great"))
    monkeypatch.setattr(tailor_pipeline, "tailor", lambda job, profile: (TailoredResume(summary="ok", cover_letter="hi"), []))
    monkeypatch.setattr(tailor_pipeline, "render_resume_pdf", lambda t, p, out: (out.parent.mkdir(parents=True, exist_ok=True), out.write_bytes(b"%PDF"), out)[-1])
    monkeypatch.setattr(tailor_pipeline, "render_cover_letter_pdf", lambda t, j, p, out: (out.parent.mkdir(parents=True, exist_ok=True), out.write_bytes(b"%PDF"), out)[-1])
    monkeypatch.setitem(submit_pipeline.ADAPTERS, "greenhouse", _FakeAdapter(adapter_result))
    import app.submit.greenhouse_api as gha
    monkeypatch.setattr(gha, "build_and_submit", lambda job, profile, settings: (adapter_result, {}))


@pytest.mark.asyncio
async def test_full_dry_run_cycle_and_idempotency(monkeypatch):
    session = _session()
    session.add(Profile(full_name="Jane", raw_text="engineer", skills=["python"]))
    session.commit()
    # Control defaults: dry_run True. Adapter returns DryRunFilled.
    _wire_stubs(monkeypatch, adapter_result=DryRunFilled())

    run = await orch.run_cycle(session)
    assert run.discovered == 2
    assert run.ranked == 2
    assert run.tailored == 2
    assert run.submitted == 0  # dry run

    apps = session.exec(select(Application)).all()
    assert len(apps) == 2
    assert all(a.status == ApplicationStatus.queued.value for a in apps)  # dry-run -> queued
    assert all(a.resume_path and a.cover_letter_path for a in apps)

    # Second cycle: same raws -> deduped, nothing new ranked/tailored/submitted.
    run2 = await orch.run_cycle(session)
    assert run2.deduped == 2
    assert run2.ranked == 0
    assert run2.tailored == 0
    assert len(session.exec(select(Job)).all()) == 2
    assert len(session.exec(select(Application)).all()) == 2


@pytest.mark.asyncio
async def test_kill_switch_blocks_submission_in_cycle(monkeypatch):
    session = _session()
    session.add(Profile(full_name="Jane", raw_text="engineer", skills=["python"]))
    session.commit()
    update_control(session, dry_run=False, submit_kill_switch=True)
    _wire_stubs(monkeypatch, adapter_result=Submitted())

    run = await orch.run_cycle(session)
    assert run.tailored == 2
    assert run.submitted == 0  # kill switch engaged

    apps = session.exec(select(Application)).all()
    # Stayed at 'tailored' because the submit stage was a no-op.
    assert all(a.status == ApplicationStatus.tailored.value for a in apps)
