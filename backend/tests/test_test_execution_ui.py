"""Compiled dashboard smoke tests for the embedded employer browser and profile review."""
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import expect
from sqlmodel import Session, select

from app.models import Job, Profile
from app.submit.assist_session import AssistSession
from tests.local_fixtures import browser_page, candidate, local_ats_origin  # noqa: F401
from tests.test_test_apply import setup


@pytest.fixture
def dashboard(setup, tmp_path, monkeypatch, local_ats_origin):
    engine, settings = setup
    from app.submit import assist_session
    from app.web import api, assist_ws, test_executions

    worker = AssistSession(
        user_data_dir=str(tmp_path / "worker-browser"),
        headless=True,
        channel="chromium",
    )
    monkeypatch.setattr(assist_session, "_session", lambda: worker)
    monkeypatch.setattr(api, "engine", engine)
    monkeypatch.setattr(test_executions, "engine", engine)
    monkeypatch.setattr(api, "get_settings", lambda: settings)

    with Session(engine) as session:
        job = session.get(Job, 1)
        job.title = "Basic UI test role"
        job.apply_url = local_ats_origin + "/test-ats/basic"
        session.add(job)
        session.commit()

    app = FastAPI()
    app.include_router(api.router)
    app.include_router(test_executions.router)
    app.include_router(assist_ws.router)
    dist = Path(__file__).resolve().parents[2] / "frontend/dist"
    assert dist.is_dir(), "Build frontend before running dashboard integration tests"

    @app.get("/ui/jobs", include_in_schema=False)
    def jobs_spa_entry():
        return FileResponse(dist / "index.html")

    @app.get("/ui/applications", include_in_schema=False)
    def applications_spa_entry():
        return FileResponse(dist / "index.html")

    @app.get("/ui/profile", include_in_schema=False)
    def profile_spa_entry():
        return FileResponse(dist / "index.html")

    app.mount("/ui", StaticFiles(directory=dist, html=True))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    yield origin, engine
    worker.close()
    server.should_exit = True
    thread.join(timeout=10)
    sock.close()


def test_jobs_opens_actual_apply_url_in_embedded_browser(dashboard, browser_page):
    origin, _ = dashboard
    page = browser_page
    page.goto(origin + "/ui/jobs")
    card = page.get_by_text("Basic UI test role", exact=True).locator("xpath=ancestor::*[@data-slot='card']")

    with page.expect_response(
        lambda response: response.url.endswith("/api/applications/1/assist")
    ) as started:
        card.get_by_role("button", name="Apply", exact=True).click()

    assert started.value.ok
    browser = page.get_by_role("region", name="Application browser for Basic UI test role")
    expect(browser).to_be_visible()
    expect(browser.get_by_role("button", name="Go back", exact=True)).to_be_visible()
    expect(browser.get_by_role("button", name="Go forward", exact=True)).to_be_visible()
    expect(browser.get_by_role("button", name="Resume automation", exact=True)).to_be_visible()
    expect(page.get_by_alt_text("Live employer application")).to_be_visible(timeout=45000)
    expect(browser.get_by_role("slider", name="Employer page vertical scroll", exact=True)).to_be_visible(timeout=45000)
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(card.get_by_role("button", name="Application open", exact=True)).to_be_visible()
    page.get_by_role("button", name="Close browser", exact=True).click()


def test_removed_applications_route_redirects_to_jobs(dashboard, browser_page):
    origin, _ = dashboard
    page = browser_page
    page.goto(origin + "/ui/applications")
    expect(page.get_by_role("heading", name="Jobs", exact=True)).to_be_visible(timeout=5000)
    expect(page.get_by_role("link", name="Applications", exact=True)).to_have_count(0)


def test_candidate_profile_can_be_corrected_approved_and_reloaded(dashboard, browser_page):
    origin, engine = dashboard
    page = browser_page
    page.goto(origin + "/ui/")
    page.get_by_role("link", name="Candidate profile", exact=True).click()

    expect(page.get_by_role("alert")).to_contain_text("Review required", timeout=5000)
    page.get_by_label("Full name").fill("Reviewed Candidate")
    page.get_by_role("button", name="Approve profile", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Approved understanding", timeout=5000)

    with Session(engine) as session:
        assert session.exec(select(Profile)).one().full_name == "Reviewed Candidate"

    page.reload(wait_until="domcontentloaded")
    expect(page.get_by_label("Full name")).to_have_value("Reviewed Candidate", timeout=5000)
    expect(page.get_by_role("alert")).to_contain_text("Approved understanding")
