"""Assisted co-browsing: fill logic, managed session/queue, live streaming, input.

Playwright runs headless against a file:// fixture only — never a real site. The
fixture mimics Ashby's DOM: inputs whose id/name equal the field *path*, titles in
plain (non-<label>) elements, and a Yes/No choice rendered as buttons.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_assist.db")

from sqlmodel import Session, SQLModel  # noqa: E402

import app.web.intervention as intervention  # noqa: E402
from app.db import engine  # noqa: E402
from app.models import Application, ApplicationStatus, Job  # noqa: E402
from app.submit import assist_session  # noqa: E402
from app.submit.ashby_assist import fill_form  # noqa: E402

_FORM = """<!doctype html><html><body>
<form onsubmit="return false">
  <div>Legal Name</div>
  <input id="_systemfield_name" name="_systemfield_name" placeholder="Type here...">
  <div>Email</div>
  <input id="_systemfield_email" name="_systemfield_email" type="email" placeholder="hello@example.com">
  <div class="field">
    <div>Are you authorized to work in the US?</div>
    <button type="button" onclick="this.setAttribute('data-selected','1')">Yes</button>
    <button type="button" onclick="this.setAttribute('data-selected','1')">No</button>
  </div>
  <div>Resume</div>
  <input id="_systemfield_resume" type="file">
  <button type="submit">Submit</button>
</form></body></html>"""

# Answer rows as produced by ashby_api._preview: name == field path.
_ANSWERS = [
    {"label": "Legal Name", "name": "_systemfield_name", "type": "String", "answer": "Alex Dev"},
    {"label": "Email", "name": "_systemfield_email", "type": "Email", "answer": "alex@example.com"},
    {"label": "Are you authorized to work in the US?", "name": "q_auth",
     "type": "ValueSelect", "answer": "Yes"},
    {"label": "Resume", "name": "_systemfield_resume", "type": "File", "answer": "(attached)"},
]


def _fixture(tmp_path: Path) -> tuple[str, str]:
    page = tmp_path / "form.html"
    page.write_text(_FORM, encoding="utf-8")
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 dummy")
    return page.as_uri(), str(resume)


def _wait_until(fn, timeout=40) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


# --- fill logic (real, headless, local Ashby-shaped fixture) ------------

def test_fill_by_path_and_choice(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    uri, resume = _fixture(tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(uri, wait_until="load")
        filled, missed = fill_form(page, _ANSWERS, resume)
        # text fields addressed by id/name == path
        assert page.locator("#_systemfield_name").input_value() == "Alex Dev"
        assert page.locator("#_systemfield_email").input_value() == "alex@example.com"
        # Yes/No choice: the "Yes" button was clicked
        assert page.get_by_role("button", name="Yes", exact=True).get_attribute("data-selected") == "1"
        assert page.locator("#_systemfield_resume").input_value().endswith("resume.pdf")
        browser.close()
    assert filled == 4 and missed == []  # resume + name + email + choice


def test_apply_input_forwards_mouse_and_keys(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    uri, _ = _fixture(tmp_path)
    sess = assist_session.AssistSession(user_data_dir=str(tmp_path / "p"), headless=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(uri, wait_until="load")
        page.locator("#_systemfield_name").click()  # focus
        sess._apply_input(page, {"type": "text", "text": "Zoe"})
        assert page.locator("#_systemfield_name").input_value() == "Zoe"
        sess._apply_input(page, {"type": "key", "key": "Backspace"})
        assert page.locator("#_systemfield_name").input_value() == "Zo"
        browser.close()


# --- managed session: sequential + persistent context reuse -------------

def test_session_processes_sequentially_and_reuses_context(tmp_path: Path, monkeypatch):
    uri, resume = _fixture(tmp_path)
    seen = []
    real = assist_session.open_and_fill

    def spy(context, *a, **k):
        seen.append(id(context))
        return real(context, *a, **k)

    monkeypatch.setattr(assist_session, "open_and_fill", spy)
    sess = assist_session.AssistSession(
        user_data_dir=str(tmp_path / "profile"), headless=True, wait_for_user=False)
    try:
        sess.enqueue(1, uri, _ANSWERS, resume)
        sess.enqueue(2, uri, _ANSWERS, resume)
        assert _wait_until(lambda: sess.snapshot(1).get("done") and sess.snapshot(2).get("done"))
        assert sess.snapshot(1)["filled"] == 4 and sess.snapshot(2)["filled"] == 4
        assert len(seen) == 2 and len(set(seen)) == 1  # same persistent context reused
    finally:
        sess.close()


def test_single_active_others_queue(tmp_path: Path):
    uri, resume = _fixture(tmp_path)
    release = threading.Event()
    sess = assist_session.AssistSession(
        user_data_dir=str(tmp_path / "profile"), headless=True, wait_for_user=True)
    sess._serve_live = lambda page, app_id: release.wait(15)  # user "finishes" on our signal
    try:
        sess.enqueue(1, uri, _ANSWERS, resume)
        sess.enqueue(2, uri, _ANSWERS, resume)
        assert _wait_until(lambda: sess.snapshot(1).get("stage") == "live")
        assert sess.snapshot(2).get("stage") == "queued"  # single active tab
        release.set()
        assert _wait_until(lambda: sess.snapshot(1).get("done") and sess.snapshot(2).get("done"))
    finally:
        release.set()
        sess.close()


def test_live_streaming_emits_jpeg_frames(tmp_path: Path):
    uri, resume = _fixture(tmp_path)
    frames: list[bytes] = []
    sess = assist_session.AssistSession(
        user_data_dir=str(tmp_path / "profile"), headless=True, wait_for_user=True,
        keep_open_seconds=30, frame_interval=0.05)
    sess.set_frame_sink(1, frames.append)
    try:
        sess.enqueue(1, uri, _ANSWERS, resume)
        assert _wait_until(lambda: len(frames) >= 1, timeout=30)
        assert frames[0][:2] == b"\xff\xd8"  # JPEG magic
        sess.stop_live(1)
        assert _wait_until(lambda: sess.snapshot(1).get("done"))
    finally:
        sess.close()


# --- endpoints ----------------------------------------------------------

def _seed_needs_human(preview_answers) -> int:
    import uuid

    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        job = Job(source="ashby", source_job_id=uuid.uuid4().hex, company="Ramp", title="AI Eng",
                  dedup_hash=uuid.uuid4().hex, ats_type="ashby",
                  apply_url="https://jobs.ashbyhq.com/ramp/j/application")
        s.add(job)
        s.commit()
        app = Application(
            job_id=job.id, status=ApplicationStatus.needs_human.value, match_score=85,
            resume_path="r.pdf", needs_human_reason="captcha",
            events=[{"event": "application_preview", "preview": {"answers": preview_answers}}],
        )
        s.add(app)
        s.commit()
        return app.id


def test_live_endpoint_enqueues_and_returns_panel(monkeypatch):
    from fastapi.testclient import TestClient

    app_id = _seed_needs_human(_ANSWERS)
    captured = {}
    monkeypatch.setattr(assist_session, "snapshot", lambda aid: {})  # not active -> enqueue
    monkeypatch.setattr(assist_session, "enqueue",
                        lambda aid, url, answers, rp: captured.update(id=aid, answers=answers))

    from app.main import app as fastapi_app
    with TestClient(fastapi_app) as client:
        r = client.get(f"/intervention/{app_id}/live")
        assert r.status_code == 200
        assert f"/ws/assist/{app_id}" in r.text          # panel wires the websocket
        assert f"screen-{app_id}" in r.text              # streamed <img>
        assert captured["id"] == app_id
        assert captured["answers"] == _ANSWERS

    with Session(engine) as s:
        s.delete(s.get(Application, app_id))
        s.commit()
