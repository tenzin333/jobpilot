"""WebSocket bridge for the in-page co-browsing view.

Frames (JPEG bytes) flow server -> client; input events (JSON) flow client ->
server and are forwarded to the live Playwright page. The browser runs on the
assist worker thread; here we only wire its frame-sink + input queue to the socket.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.submit import assist_session

router = APIRouter()
log = logging.getLogger("assist.ws")


@router.websocket("/ws/assist/{app_id}")
async def ws_assist(ws: WebSocket, app_id: int) -> None:
    await ws.accept()
    loop = asyncio.get_running_loop()
    pending: dict = {"fut": None}

    def sink(data: bytes) -> None:
        # Called from the worker thread. Drop frames if the previous send is still
        # in flight (avoid backlog on a slow client).
        fut = pending["fut"]
        if fut is not None and not fut.done():
            return
        try:
            pending["fut"] = asyncio.run_coroutine_threadsafe(ws.send_bytes(data), loop)
        except Exception:
            pass

    assist_session.set_frame_sink(app_id, sink)
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "done":
                assist_session.stop_live(app_id)
                break
            assist_session.push_input(app_id, msg)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("assist ws error (app %s): %s", app_id, exc)
    finally:
        assist_session.clear_frame_sink(app_id)
        # Closing the panel ends the live session only if THIS app is the active
        # one (a queued panel closing must not kill the live job).
        assist_session.stop_live(app_id)
