"""Phase 5 acceptance: form classification, real Playwright fill/submit, orchestration.

Real submission runs against LOCAL file:// HTML fixtures only — never a real site.
"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

from app.config import Preferences, Settings, SourceConfig  # noqa: E402
from app.models import Application, ApplicationStatus, Job, Profile  # noqa: E402
from app.pipeline import submit as submit_pipeline  # noqa: E402
from app.submit.base import (  # noqa: E402
    DryRunFilled,
    FieldMap,
    NeedsHuman,
    Submitted,
    run_form_submit,
)
from app.submit.detect import classify_form  # noqa: E402

# --- pure classifier ----------------------------------------------------

def test_classify_clean_form():
    assert classify_form("<form><input name='email'></form>") is None


def test_classify_captcha():
    assert classify_form("<div class='g-recaptcha' data-sitekey='x'></div>") == "captcha"


def test_classify_video():
    assert classify_form("Please record a video introduction") == "video introduction required"


def test_classify_essay_phrase_and_required_textarea():
    assert classify_form("Why do you want to work here?") == "free-text essay required"
    assert classify_form("<textarea required></textarea>") == "free-text essay required"


# --- real Playwright fill/submit on local fixtures ----------------------

_SUPPORTED = """<!doctype html><html><body>
<form onsubmit="return false">
  <input name="full_name">
  <input type="email" name="email">
  <input type="file" name="resume">
  <button type="submit">Submit</button>
</form></body></html>"""

_CAPTCHA = """<!doctype html><html><body>
<form><input name="email"><div class="g-recaptcha" data-sitekey="x"></div>
<button type="submit">Submit</button></form></body></html>"""

_FIELD_MAP = FieldMap(
    name=["input[name*='name']"],
    email=["input[type='email']"],
    resume_file=["input[type='file']"],
    submit=["button[type='submit']"],
)


def _profile() -> Profile:
    return Profile(full_name="Jane Engineer", email="jane@example.com")


def test_run_form_submit_dry_run(tmp_path: Path):
    page = tmp_path / "supported.html"
    page.write_text(_SUPPORTED, encoding="utf-8")
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 dummy")

    result = run_form_submit(
        apply_url=page.as_uri(), profile=_profile(), resume_path=str(resume),
        dry_run=True, field_map=_FIELD_MAP,
    )
    assert isinstance(result, DryRunFilled)  # filled but not clicked


def test_run_form_submit_live(tmp_path: Path):
    page = tmp_path / "supported.html"
    page.write_text(_SUPPORTED, encoding="utf-8")
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 dummy")

    result = run_form_submit(
        apply_url=page.as_uri(), profile=_profile(), resume_path=str(resume),
        dry_run=False, field_map=_FIELD_MAP,
    )
    assert isinstance(result, Submitted)


def test_run_form_submit_captcha_needs_human(tmp_path: Path):
    page = tmp_path / "captcha.html"
    page.write_text(_CAPTCHA, encoding="utf-8")

    result = run_form_submit(
        apply_url=page.as_uri(), profile=_profile(), resume_path="",
        dry_run=False, field_map=_FIELD_MAP,
    )
    assert isinstance(result, NeedsHuman)
    assert result.reason == "captcha"


# --- orchestration ------------------------------------------------------

class _FakeAdapter:
    name = "greenhouse"
    field_map = _FIELD_MAP

    def __init__(self, result):
        self._result = result
        self.calls = 0

    def submit(self, **kwargs):
        self.calls += 1
        return self._result


def _setup_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _tailored_app(session: Session, source="greenhouse", ats="greenhouse") -> Application:
    job = Job(source=source, source_job_id="1", company="Acme", title="Eng",
              dedup_hash=f"h{source}{session.exec(select(Job)).all().__len__()}", ats_type=ats, apply_url="https://x")
    session.add(job)
    session.commit()
    app = Application(job_id=job.id, status=ApplicationStatus.tailored.value, match_score=90, resume_path="r.pdf")
    session.add(app)
    session.commit()
    return app


def _prefs(autonomy=True) -> Preferences:
    return Preferences(sources={
        "greenhouse": SourceConfig(enabled=True, autonomy=autonomy, companies=["acme"]),
    })


def test_kill_switch_blocks_all(monkeypatch):
    session = _setup_session()
    _tailored_app(session)
    settings = Settings(dry_run=False, submit_kill_switch=True)
    stats = submit_pipeline.submit_tailored(session, _profile(), _prefs(), settings)
    assert stats == {"submitted": 0, "dry_run": 0, "needs_human": 0, "failed": 0, "skipped_cap": 0}


def test_autonomy_off_routes_to_human(monkeypatch):
    session = _setup_session()
    app = _tailored_app(session)
    monkeypatch.setitem(submit_pipeline.ADAPTERS, "greenhouse", _FakeAdapter(Submitted()))
    settings = Settings(dry_run=False)
    stats = submit_pipeline.submit_tailored(session, _profile(), _prefs(autonomy=False), settings)
    assert stats["needs_human"] == 1
    session.refresh(app)
    assert app.status == ApplicationStatus.needs_human.value
    assert app.needs_human_reason == "source autonomy disabled"


def test_dry_run_maps_to_queued(monkeypatch):
    session = _setup_session()
    app = _tailored_app(session)
    import app.submit.greenhouse_api as gha
    monkeypatch.setattr(gha, "build_and_submit", lambda job, profile, settings: (DryRunFilled(), {}))
    settings = Settings(dry_run=True)
    stats = submit_pipeline.submit_tailored(session, _profile(), _prefs(), settings)
    assert stats["dry_run"] == 1
    session.refresh(app)
    assert app.status == ApplicationStatus.queued.value


def test_live_submit_and_daily_cap(monkeypatch):
    session = _setup_session()
    a1 = _tailored_app(session)
    a2 = _tailored_app(session)
    import app.submit.greenhouse_api as gha
    monkeypatch.setattr(gha, "build_and_submit", lambda job, profile, settings: (Submitted(), {}))
    settings = Settings(dry_run=False, daily_submit_cap=1)  # only 1 allowed today
    stats = submit_pipeline.submit_tailored(session, _profile(), _prefs(), settings)
    assert stats["submitted"] == 1
    assert stats["skipped_cap"] == 1


def test_unsupported_ats_needs_human(monkeypatch):
    session = _setup_session()
    app = _tailored_app(session, source="career_page", ats="career_page")
    prefs = Preferences(sources={"career_page": SourceConfig(enabled=True, autonomy=True)})
    settings = Settings(dry_run=False)
    stats = submit_pipeline.submit_tailored(session, _profile(), prefs, settings)
    assert stats["needs_human"] == 1
    session.refresh(app)
    assert app.needs_human_reason == "unsupported ATS"
