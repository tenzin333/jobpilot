import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.candidate_brain.services import AnswerDraft, AnswerResolution
from app.config import Settings
from app.ghost_cursor.models import Confirmation
from app.models import Application, Control, Job, Profile, TestExecution as Execution, utcnow
from app.pipeline.test_apply import Coordinator, ExecutionError
from app.submit.assist_session import AssistSession
from tests.local_fixtures import candidate, local_ats_origin


class QueueOnly:
    def __init__(self):
        self.tasks = []

    def enqueue_task(self, app_id, callback):
        self.tasks.append(callback)


def test_managed_session_always_uses_embedded_headless_browser(setup, monkeypatch):
    _, settings = setup
    from app.submit import assist_session
    settings.assist_headless = False
    settings.test_ats_enabled = False
    monkeypatch.setattr(assist_session, "get_settings", lambda: settings)
    monkeypatch.setattr(assist_session, "_SESSION", None)
    assert assist_session._session().headless is True


@pytest.fixture
def setup(tmp_path, monkeypatch, candidate, local_ats_origin):
    engine = create_engine(f"sqlite:///{tmp_path / 'executions.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    settings = Settings(_env_file=None, test_ats_enabled=True, test_ats_base_url=local_ats_origin,
                        submit_kill_switch=False, dry_run=False)
    monkeypatch.setattr("app.controls.get_settings", lambda: settings)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.submit.assist_session.get_settings", lambda: settings)
    with Session(engine) as session:
        session.add(Control(dry_run=False, submit_kill_switch=False))
        session.add(Profile(first_name="Alex", last_name="Candidate", email="alex@example.com", phone="+12025550123",
                            answer_bank=candidate))
        for i in range(1, 5):
            session.add(Job(id=i, dedup_hash=str(i), apply_url="https://employer.invalid/apply", title="Synthetic role", ats_type="greenhouse"))
            session.add(Application(id=i, job_id=i, status="tailored", resume_path=candidate["resume"], cover_letter_path=candidate["resume"]))
        session.commit()
    yield engine, settings
    engine.dispose()


def wait_for(coordinator, execution_id, states, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = coordinator.status(execution_id)
        if result["state"] in states:
            return result
        if result["state"] == "failed":
            pytest.fail(str(result))
        time.sleep(.05)
    pytest.fail(f"Timed out: {coordinator.status(execution_id)}")


def test_concurrent_start_and_confirmation(setup):
    engine, _ = setup
    worker = QueueOnly()
    coordinator = Coordinator(engine, worker)
    with ThreadPoolExecutor(max_workers=6) as pool:
        starts = list(pool.map(lambda _: coordinator.start(1), range(6)))
    assert len({r[0]["execution_id"] for r in starts}) == 1
    assert sum(created for _, created in starts) == 1
    assert len(worker.tasks) == 1
    execution_id = starts[0][0]["execution_id"]
    with Session(engine) as session:
        row = session.get(Execution, execution_id)
        row.state, row.review_version = "ready_for_review", 1
        session.add(row); session.commit()
    with pytest.raises(ExecutionError):
        coordinator.confirm(execution_id, Confirmation(review_version=2))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: coordinator.confirm(execution_id, Confirmation(review_version=1)), range(4)))
    assert {r["state"] for r in results} == {"confirming"}
    assert len(worker.tasks) == 1


@pytest.mark.parametrize("app_id", [1, 2, 3, 4])
def test_worker_completion_keeps_real_status_unchanged(setup, tmp_path, app_id):
    engine, _ = setup
    worker = AssistSession(user_data_dir=str(tmp_path / "browser"), headless=True, channel="chromium")
    coordinator = Coordinator(engine, worker)
    try:
        result, _ = coordinator.start(app_id)
        execution_id = result["execution_id"]
        review = wait_for(coordinator, execution_id, {"ready_for_review"})
        assert review["can_confirm"]
        coordinator.confirm(execution_id, Confirmation(review_version=review["review_version"]))
        done = wait_for(coordinator, execution_id, {"succeeded"})
        assert done["receipt"]["execution_id"] == execution_id
        assert coordinator.confirm(execution_id, Confirmation(review_version=1))["state"] == "succeeded"
        with Session(engine) as session:
            app = session.get(Application, app_id)
            assert app.status == "tailored" and app.submitted_at is None
    finally:
        worker.close()


def test_candidate_brain_answer_flows_into_ghost_cursor(setup, tmp_path):
    engine, _ = setup
    with Session(engine) as session:
        profile = session.exec(select(Profile)).one()
        profile.answer_bank = {key: value for key, value in profile.answer_bank.items()
                               if key != "achievement"}
        session.add(profile)
        session.commit()

    seen = []

    def resolve(profile, job, flow, bindings):
        seen.append((profile.email, job.title, flow.name, "achievement" in bindings))
        return AnswerResolution(
            answers={"achievement": "Built a CV-grounded local test tool."},
            evidence={"achievement": ["CV1"]},
            drafts=[AnswerDraft(field="achievement", question="Describe one relevant achievement",
                evidence_ids=["CV1"], evidence_snippets=["Built a local test tool from the CV."])],
        )

    worker = AssistSession(user_data_dir=str(tmp_path / "browser"), headless=True, channel="chromium")
    coordinator = Coordinator(engine, worker, answer_resolver=resolve)
    try:
        started, _ = coordinator.start(2)
        review = wait_for(coordinator, started["execution_id"], {"ready_for_review"})
        assert review["state"] == "ready_for_review"
        assert seen == [("alex@example.com", "Synthetic role", "multistep", False)]
        with Session(engine) as session:
            row = session.get(Execution, started["execution_id"])
            trace = next(item for item in row.trace if item["reason"] == "candidate_brain_grounded:CV1")
            assert trace["selected"] == "Describe one relevant achievement"
            assert trace["candidates"] == ["Built a local test tool from the CV."]
        coordinator.cancel(started["execution_id"])
    finally:
        worker.close()


def test_controls_expiry_restart_cancel_and_bulk(setup):
    engine, _ = setup
    coordinator = Coordinator(engine, QueueOnly())
    first, _ = coordinator.start(1)
    execution_id = first["execution_id"]
    with Session(engine) as session:
        row = session.get(Execution, execution_id)
        row.state, row.review_version = "ready_for_review", 1
        session.add(row)
        control = session.exec(select(Control)).one()
        control.dry_run = True
        session.add(control); session.commit()
    with pytest.raises(ExecutionError, match="Dry run"):
        coordinator.confirm(execution_id, Confirmation(review_version=1))
    assert not coordinator.status(execution_id)["can_confirm"]
    with Session(engine) as session:
        control = session.exec(select(Control)).one()
        control.submit_kill_switch = True
        session.add(control); session.commit()
    with pytest.raises(ExecutionError, match="Kill switch"):
        coordinator.start(2)
    coordinator.cancel(execution_id)
    assert coordinator.get(execution_id).active_application_id is None
    with Session(engine) as session:
        control = session.exec(select(Control)).one()
        control.submit_kill_switch = False
        session.add(control); session.commit()
    second, _ = coordinator.start(1)
    with Session(engine) as session:
        row = session.get(Execution, second["execution_id"])
        row.state, row.expires_at = "ready_for_review", utcnow() - timedelta(seconds=1)
        session.add(row); session.commit()
    assert coordinator.get(second["execution_id"]).state == "expired"
    assert coordinator.bulk()["queued_test"] == 4
    assert coordinator.bulk()["already_active"] == 4
    coordinator.recover()
    with Session(engine) as session:
        assert not session.exec(select(Execution).where(Execution.active_application_id != None)).all()


def test_start_replaces_an_expired_active_execution_in_one_request(setup):
    engine, _ = setup
    worker = QueueOnly()
    coordinator = Coordinator(engine, worker)
    first, _ = coordinator.start(1)
    with Session(engine) as session:
        row = session.get(Execution, first["execution_id"])
        row.state = "needs_human"
        row.expires_at = utcnow() - timedelta(seconds=1)
        session.add(row)
        session.commit()

    restarted, created = coordinator.start(1)

    assert created
    assert restarted["execution_id"] != first["execution_id"]
    assert restarted["state"] == "queued"
    assert len(worker.tasks) == 2


def test_preparation_failure_never_opens_browser(setup, monkeypatch):
    engine, _ = setup
    worker = QueueOnly()
    coordinator = Coordinator(engine, worker)
    with Session(engine) as session:
        app = session.get(Application, 1)
        app.resume_path = "missing.pdf"
        session.add(app); session.commit()
    monkeypatch.setattr("app.pipeline.test_apply.tailor_application", lambda *a: (_ for _ in ()).throw(RuntimeError("model unavailable")))
    result, _ = coordinator.start(1)
    class Owner:
        import threading
        _stop = threading.Event()
        def _ensure_browser(self):
            pytest.fail("Browser must not start after preparation failed")
    worker.tasks[0](Owner())
    assert coordinator.get(result["execution_id"]).reason_code == "artifact_preparation_failed"


def test_intervention_then_cancel_releases_serial_browser(setup, tmp_path):
    engine, _ = setup
    with Session(engine) as session:
        profile = session.exec(select(Profile)).one()
        authorization_keys = {"work_authorization", "work authorization", "authorized",
                              "authorized to work", "sponsorship", "require sponsorship"}
        profile.answer_bank = {k: v for k, v in profile.answer_bank.items() if k not in authorization_keys}
        session.add(profile); session.commit()
    worker = AssistSession(user_data_dir=str(tmp_path / "browser"), headless=True, channel="chromium")
    coordinator = Coordinator(engine, worker)
    try:
        first, _ = coordinator.start(1)
        second, _ = coordinator.start(2)
        missing = wait_for(coordinator, first["execution_id"], {"needs_human"})
        assert missing["unresolved_fields"] == ["work_authorization"]
        assert coordinator.status(second["execution_id"])["state"] == "queued"
        coordinator.intervene(first["execution_id"], {"work_authorization": "Require sponsorship"})
        wait_for(coordinator, first["execution_id"], {"ready_for_review"})
        coordinator.cancel(first["execution_id"])
        second_missing = wait_for(coordinator, second["execution_id"], {"needs_human"})
        assert second_missing["unresolved_fields"] == ["authorized"]
        coordinator.intervene(second["execution_id"], {"authorized": "Yes"})
        wait_for(coordinator, second["execution_id"], {"ready_for_review"})
        coordinator.cancel(second["execution_id"])
    finally:
        worker.close()


def test_kill_switch_changed_at_review_blocks_worker(setup, tmp_path):
    engine, _ = setup
    worker = AssistSession(user_data_dir=str(tmp_path / "browser"), headless=True, channel="chromium")
    coordinator = Coordinator(engine, worker)
    try:
        row, _ = coordinator.start(1)
        wait_for(coordinator, row["execution_id"], {"ready_for_review"})
        with Session(engine) as session:
            control = session.exec(select(Control)).one()
            control.submit_kill_switch = True
            session.add(control); session.commit()
        failed = wait_for(coordinator, row["execution_id"], {"failed"})
        assert "Kill switch" in failed["reason_code"]
        assert coordinator.get(row["execution_id"]).receipt == {}
    finally:
        worker.close()


@pytest.mark.parametrize("redirect", [False, True])
def test_external_request_and_redirect_never_reach_destination(setup, tmp_path, redirect):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    engine, settings = setup
    received = []
    class Destination(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            self.send_response(200); self.end_headers()
        def log_message(self, *args):
            pass
    destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)
    target = f"http://127.0.0.1:{destination.server_port}/external"
    class Source(BaseHTTPRequestHandler):
        def do_GET(self):
            if redirect:
                self.send_response(302); self.send_header("Location", target); self.end_headers()
            else:
                self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
                self.wfile.write(f'<html><img src="{target}"></html>'.encode())
        def log_message(self, *args):
            pass
    source = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    for server in [source, destination]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    settings.test_ats_base_url = f"http://127.0.0.1:{source.server_port}"
    worker = AssistSession(user_data_dir=str(tmp_path / "browser"), headless=True, channel="chromium")
    coordinator = Coordinator(engine, worker)
    try:
        row, _ = coordinator.start(1)
        failed = wait_for(coordinator, row["execution_id"], {"failed"})
        assert failed["reason_code"] in {"destination_blocked", "browser_execution_failed"}
        assert received == []
    finally:
        worker.close()
        for server in [source, destination]:
            server.shutdown(); server.server_close()


def test_disconnected_browser_allows_a_new_execution(setup, tmp_path):
    engine, _ = setup
    worker = AssistSession(user_data_dir=str(tmp_path / "browser"), headless=True, channel="chromium")
    coordinator = Coordinator(engine, worker)
    closed = []
    def disconnect_once(key, page, **_kwargs):
        if not closed:
            closed.append(True)
            worker._context.close()  # Runs on the owning thread, like a browser disconnect.
    worker.emit_frame = disconnect_once
    try:
        first, _ = coordinator.start(1)
        wait_for(coordinator, first["execution_id"], {"failed"})
        second, _ = coordinator.start(1)
        wait_for(coordinator, second["execution_id"], {"ready_for_review"})
        coordinator.cancel(second["execution_id"])
    finally:
        worker.close()
