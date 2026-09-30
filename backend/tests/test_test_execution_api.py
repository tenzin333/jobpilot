import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, select

from app.models import Application, Control, TestExecution as Execution
from app.pipeline.test_apply import Coordinator
from tests.test_test_apply import QueueOnly, setup
from tests.local_fixtures import candidate, local_ats_origin


@pytest.fixture
def api_client(setup, monkeypatch):
    engine, settings = setup
    from app.web import api, assist_ws, test_executions
    from app.submit import assist_session
    worker = QueueOnly()
    worker._frame_sinks = {}
    worker.assist_jobs = []
    worker.assist_inputs = []
    worker.stopped = []
    worker.set_frame_sink = lambda key, sink: worker._frame_sinks.update({key: sink})
    worker.clear_frame_sink = lambda key: worker._frame_sinks.pop(key, None)
    worker.snapshot = lambda app_id: {"stage": "queued"} if worker.assist_jobs else {}
    worker.enqueue = lambda *args: worker.assist_jobs.append(args)
    worker.push_input = lambda *args: worker.assist_inputs.append(args)
    worker.stop_live = lambda app_id: worker.stopped.append(app_id)
    monkeypatch.setattr(assist_session, "_session", lambda: worker)
    monkeypatch.setattr(api, "engine", engine)
    monkeypatch.setattr(test_executions, "engine", engine)
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(test_executions.router)
    app.include_router(assist_ws.router)
    with TestClient(app) as client:
        yield client, engine, worker


def test_start_retry_bulk_status_and_real_assist(api_client):
    client, engine, worker = api_client
    response = client.post("/api/applications/1/test-executions")
    assert response.status_code == 202
    execution_id = response.json()["execution_id"]
    assert client.post("/api/applications/1/test-executions").status_code == 200
    assert client.post("/api/matches/1/apply").json()["test_execution"]["execution_id"] == execution_id
    assert client.post("/api/matches/1/retry").json()["test_execution"]["execution_id"] == execution_id
    assert len(worker.tasks) == 1
    assert client.post("/api/applications/submit").json()["queued_test"] == 3
    rows = client.get("/api/applications").json()
    assert rows["test_mode"]
    assert rows["applications"][0]["test_execution"]
    assist = client.post("/api/applications/1/assist")
    assert assist.status_code == 200
    assert worker.assist_jobs[0][0] == 1
    assert worker.assist_jobs[0][1] == "https://employer.invalid/apply"
    assert any(answer["name"] == "first_name" for answer in worker.assist_jobs[0][2])
    assert client.post("/api/intervention/1/done").status_code == 409
    assert client.post("/api/jobs/clear").status_code == 409
    with client.websocket_connect("/ws/assist/1") as ws:
        ws.send_json({"type": "text", "text": "A"})
        ws.send_json({"type": "done"})
    assert worker.assist_inputs == [(1, {"type": "text", "text": "A"})]
    with client.websocket_connect(f"/ws/test-executions/{execution_id}") as ws:
        ws.send_json({"type": "key", "key": "Enter"})
        ws.send_json({"type": "click", "x": 600, "y": 500})
        ws.send_json({"type": "done"})
    assert Coordinator(engine).get(execution_id).state == "queued"
    assert client.post(f"/api/test-executions/{execution_id}/cancel").json()["state"] == "cancelled"


@pytest.mark.parametrize("stage", ["queued", "opening", "filling", "login_required", "live"])
def test_real_assist_does_not_requeue_an_active_session(api_client, stage):
    client, _engine, worker = api_client
    worker.snapshot = lambda _app_id: {"stage": stage}

    response = client.post("/api/applications/1/assist")

    assert response.status_code == 200
    assert response.json()["stage"] == stage
    assert worker.assist_jobs == []


def test_confirmation_and_intervention_validation(api_client):
    client, engine, _ = api_client
    execution_id = client.post("/api/applications/1/test-executions").json()["execution_id"]
    assert client.post(f"/api/test-executions/{execution_id}/confirm", json={"review_version": 1}).status_code == 409
    with Session(engine) as session:
        row = session.get(Execution, execution_id)
        row.state, row.unresolved_fields = "needs_human", ["work_authorization"]
        session.add(row); session.commit()
    assert client.post(f"/api/test-executions/{execution_id}/interventions", json={"answers": {"url": "https://employer.invalid"}}).status_code == 400
    assert client.post(f"/api/test-executions/{execution_id}/interventions", json={"answers": {"work_authorization": "Require sponsorship"}}).json()["state"] == "filling"
    with Session(engine) as session:
        row = session.get(Execution, execution_id)
        row.state, row.review_version = "ready_for_review", 2
        session.add(row)
        control = session.exec(select(Control)).one()
        control.dry_run = True
        session.add(control); session.commit()
    assert not client.get(f"/api/test-executions/{execution_id}").json()["can_confirm"]
    assert client.post(f"/api/test-executions/{execution_id}/confirm", json={"review_version": 2}).status_code == 409
    with Session(engine) as session:
        control = session.exec(select(Control)).one()
        control.dry_run = False
        session.add(control); session.commit()
    assert client.post(f"/api/test-executions/{execution_id}/confirm", json={"review_version": 1}).status_code == 409
    assert client.post(f"/api/test-executions/{execution_id}/confirm", json={"review_version": 2}).json()["state"] == "confirming"
    with Session(engine) as session:
        assert session.get(Application, 1).submitted_at is None


def test_status_restores_only_sanitized_candidate_brain_provenance(api_client):
    client, engine, _ = api_client
    execution_id = client.post("/api/applications/1/test-executions").json()["execution_id"]
    with Session(engine) as session:
        row = session.get(Execution, execution_id)
        row.state = "ready_for_review"
        row.trace = [
            {"step": "answer:achievement", "at": "2026-09-27T00:00:00Z",
             "candidates": ["Built a deployment platform.", "alex@example.com", "https://private.example/cv"],
             "selected": "Describe one relevant achievement", "verified": True,
             "reason": "candidate_brain_grounded:CV1,CV2"},
            {"step": "authorized", "at": "2026-09-27T00:00:01Z",
             "candidates": ["Sensitive answer"], "selected": "authorization", "verified": True,
             "reason": ""},
        ]
        session.add(row)
        session.commit()

    first = client.get(f"/api/test-executions/{execution_id}").json()
    with Session(engine) as session:
        row = session.get(Execution, execution_id)
        row.state, row.active_application_id = "cancelled", None
        session.add(row)
        session.commit()
    second = client.get(f"/api/test-executions/{execution_id}").json()

    expected = [{"field": "achievement", "question": "Describe one relevant achievement",
                 "evidence": ["Built a deployment platform."]}]
    assert first["candidate_brain_drafts"] == expected
    assert second["candidate_brain_drafts"] == expected
    assert second["state"] == "cancelled"


def test_jobs_list_loads_execution_summaries_in_bounded_queries(api_client):
    client, engine, _ = api_client
    statements = []

    def count_query(*_args):
        statements.append(1)

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        response = client.get("/api/jobs")
    finally:
        event.remove(engine, "before_cursor_execute", count_query)
    assert response.status_code == 200
    assert len(response.json()["jobs"]) == 4
    assert len(statements) <= 3
