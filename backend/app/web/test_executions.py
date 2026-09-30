"""Execution requests and attach-only, read-only browser streaming."""
import asyncio

from fastapi import APIRouter, HTTPException, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from app.db import engine
from app.ghost_cursor.models import Confirmation
from app.pipeline.test_apply import Coordinator, ExecutionError
from app.submit import assist_session

router = APIRouter()


def call(method, *args):
    try:
        return method(*args)
    except ExecutionError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.reason) from exc


@router.post("/api/applications/{app_id}/test-executions")
def start(app_id: int, response: Response):
    result, created = call(Coordinator(engine).start, app_id)
    response.status_code = 202 if created else 200
    return result


@router.get("/api/test-executions/{execution_id}")
def status(execution_id: str):
    return call(Coordinator(engine).status, execution_id)


@router.post("/api/test-executions/{execution_id}/confirm")
def confirm(execution_id: str, confirmation: Confirmation):
    return call(Coordinator(engine).confirm, execution_id, confirmation)


class Intervention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: dict[str, str | bool] = Field(default_factory=dict)
    targets: dict[str, str] = Field(default_factory=dict)


@router.post("/api/test-executions/{execution_id}/interventions")
def intervene(execution_id: str, intervention: Intervention):
    return call(Coordinator(engine).intervene, execution_id, intervention.answers, intervention.targets)


@router.post("/api/test-executions/{execution_id}/cancel")
def cancel(execution_id: str):
    return call(Coordinator(engine).cancel, execution_id)


@router.websocket("/ws/test-executions/{execution_id}")
async def stream(ws: WebSocket, execution_id: str):
    try:
        Coordinator(engine).get(execution_id)
    except ExecutionError:
        await ws.close(code=1008)
        return
    await ws.accept()
    loop = asyncio.get_running_loop()
    pending = [None]

    def sink(data):
        if pending[0] is None or pending[0].done():
            pending[0] = asyncio.run_coroutine_threadsafe(ws.send_bytes(data), loop)

    owner = assist_session._session()
    owner.set_frame_sink(execution_id, sink)
    try:
        while True:
            # Discard all raw pointer/keyboard input, including Enter and done.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if owner._frame_sinks.get(execution_id) is sink:
            owner.clear_frame_sink(execution_id)
        # A viewer disconnect does not cancel another viewer's execution.
        # Durable review/intervention expiry releases an abandoned browser slot.
