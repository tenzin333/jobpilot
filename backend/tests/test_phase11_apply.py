"""Per-card apply flow: submit_one, apply_one, and the matches endpoints."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_apply.db")

import pytest  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_apply_progress():
    # The per-app progress tracker is keyed by app_id; clear it between tests so
    # IDs reused across separate test DBs don't leak state.
    from app.pipeline import state
    state._apply_progress.clear()
    yield
    state._apply_progress.clear()

from app.config import Preferences, Settings, SourceConfig  # noqa: E402
from app.models import Application, ApplicationStatus, Job, Profile  # noqa: E402
from app.pipeline import submit as submit_pipeline  # noqa: E402
from app.pipeline.submit import submit_one  # noqa: E402
from app.submit.base import DryRunFilled, Submitted  # noqa: E402


def _session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return Session(engine)


class _FakeAdapter:
    name = "greenhouse"
    field_map = None

    def __init__(self, result):
        self._result = result

    def submit(self, **kwargs):
        return self._result


def _tailored_app(session: Session) -> tuple[Application, Profile]:
    profile = Profile(full_name="Jane", email="j@x.com")
    session.add(profile)
    job = Job(source="greenhouse", source_job_id="1", company="Acme", title="AI Engineer",
              dedup_hash="h", ats_type="greenhouse", apply_url="https://x")
    session.add(job)
    session.commit()
    app = Application(job_id=job.id, status=ApplicationStatus.tailored.value, match_score=88, resume_path="r.pdf")
    session.add(app)
    session.commit()
    return app, profile


def _prefs(autonomy=True) -> Preferences:
    return Preferences(sources={"greenhouse": SourceConfig(enabled=True, autonomy=autonomy)})


def test_submit_one_live(monkeypatch):
    session = _session()
    app, profile = _tailored_app(session)
    # Greenhouse now uses the schema-driven submitter; mock it.
    import app.submit.greenhouse_api as gha
    monkeypatch.setattr(gha, "build_and_submit", lambda job, profile, settings: (Submitted(), {}))
    outcome = submit_one(session, app, profile, _prefs(), Settings(dry_run=False))
    assert outcome == "submitted"
    session.refresh(app)
    assert app.status == ApplicationStatus.submitted.value
    assert app.submitted_at is not None


def test_submit_one_kill_switch(monkeypatch):
    session = _session()
    app, profile = _tailored_app(session)
    monkeypatch.setitem(submit_pipeline.ADAPTERS, "greenhouse", _FakeAdapter(Submitted()))
    outcome = submit_one(session, app, profile, _prefs(), Settings(submit_kill_switch=True))
    assert outcome == "blocked"
    session.refresh(app)
    assert app.status == ApplicationStatus.tailored.value  # untouched


def test_submit_one_cap(monkeypatch):
    session = _session()
    app, profile = _tailored_app(session)
    monkeypatch.setitem(submit_pipeline.ADAPTERS, "greenhouse", _FakeAdapter(Submitted()))
    outcome = submit_one(session, app, profile, _prefs(), Settings(dry_run=False), remaining_cap=0)
    assert outcome == "skipped_cap"


def test_apply_one_double_submit_guarded():
    """apply_one must not re-process an already-submitted application."""
    from app.pipeline import apply as apply_mod

    session = _session()
    app, _ = _tailored_app(session)
    app.status = ApplicationStatus.submitted.value
    session.add(app)
    session.commit()

    # Point apply_one's engine/session at our in-memory db.
    monkey_engine = session.get_bind()
    import app.pipeline.apply as a
    orig_engine = a.engine
    a.engine = monkey_engine
    try:
        result = apply_mod.apply_one(app.id)
    finally:
        a.engine = orig_engine
    assert result.startswith("already:")


def test_matches_and_status_endpoints(monkeypatch):
    from fastapi.testclient import TestClient

    # Use the app's real engine (file db) for endpoint wiring.
    from app.db import engine as app_engine
    SQLModel.metadata.create_all(app_engine)
    with Session(app_engine) as s:
        job = Job(source="greenhouse", source_job_id="z", company="Acme", title="AI Engineer",
                  dedup_hash="hz", ats_type="greenhouse", apply_url="https://x")
        s.add(job)
        s.commit()
        app_row = Application(job_id=job.id, status=ApplicationStatus.ranked.value, match_score=90)
        s.add(app_row)
        s.commit()
        app_id = app_row.id

    from app.main import app as fastapi_app
    with TestClient(fastapi_app) as client:
        r = client.get("/matches")
        assert r.status_code == 200
        assert "AI Engineer" in r.text

        # While an apply is in progress (tracker active), status shows the stage + polls.
        from app.pipeline.state import apply_set_stage, apply_start
        apply_start(app_id)
        apply_set_stage(app_id, "tailoring")
        r2 = client.get(f"/matches/{app_id}/status")
        assert r2.status_code == 200
        assert "Tailoring" in r2.text and "hx-get" in r2.text

        # With no active apply, the cell is a static badge (no infinite polling).
        from app.pipeline import state
        state._apply_progress.clear()
        r3 = client.get(f"/matches/{app_id}/status")
        assert "hx-get" not in r3.text

    # cleanup rows
    with Session(app_engine) as s:
        s.delete(s.get(Application, app_id))
        s.commit()


def test_retry_endpoint_resets_failed(monkeypatch):
    from fastapi.testclient import TestClient

    from app.db import engine as app_engine
    SQLModel.metadata.create_all(app_engine)
    with Session(app_engine) as s:
        job = Job(source="greenhouse", source_job_id="r", company="Acme", title="AI Engineer",
                  dedup_hash="hr", ats_type="greenhouse", apply_url="https://x")
        s.add(job)
        s.commit()
        row = Application(job_id=job.id, status=ApplicationStatus.failed.value,
                          match_score=85, resume_path="r.pdf", needs_human_reason="")
        s.add(row)
        s.commit()
        app_id = row.id

    import app.web.matches as m
    monkeypatch.setattr(m, "apply_one", lambda app_id: "ok")  # don't run the real pipeline

    from app.main import app as fastapi_app
    with TestClient(fastapi_app) as client:
        r = client.post(f"/matches/{app_id}/retry")
        assert r.status_code == 200
        assert "hx-get" in r.text  # apply started -> live-polling fragment

    with Session(app_engine) as s:
        # had a resume -> retried from the 'tailored' stage, not stuck on 'failed'
        assert s.get(Application, app_id).status == ApplicationStatus.tailored.value
        s.delete(s.get(Application, app_id))
        s.commit()
