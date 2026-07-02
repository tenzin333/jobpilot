"""Managed assist session: ONE persistent browser + a job queue, streamed in-page.

Captcha-gated apps (Ashby, etc.) are handed off to a single persistent Chromium
the app orchestrates (headless — no pop-up window). Each job is filled in, then
the live page is **streamed into the dashboard** (~10 fps JPEG screenshots over a
WebSocket) and the user's clicks/keystrokes are forwarded back, so the human
solves the captcha and clicks Submit inside the embedded view. Jobs are processed
one-at-a-time in the SAME context, so logins/cookies persist (fewer captchas).
We never auto-solve the captcha or auto-click submit.

Thread-safety: ALL Playwright calls happen on the single worker thread. Request /
WebSocket threads only touch the queue, the status registry, the input queue, and
the (single) frame-sink callback.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from app.config import get_settings
from app.submit.ashby_assist import open_and_fill

log = logging.getLogger("assist")

_VIEWPORT = {"width": 1280, "height": 800}
_SHUTDOWN = "__shutdown__"  # sentinel apply_url that tells the worker to tear down


@dataclass
class _Job:
    app_id: int
    apply_url: str
    answers: list = field(default_factory=list)
    resume_path: str | None = None


class AssistSession:
    def __init__(self, *, user_data_dir: str, headless: bool = True,
                 wait_for_user: bool = True, keep_open_seconds: int = 1800,
                 frame_interval: float = 0.1):
        self.user_data_dir = user_data_dir
        self.headless = headless
        self.wait_for_user = wait_for_user
        self.keep_open_seconds = keep_open_seconds
        self.frame_interval = frame_interval
        self._q: "queue.Queue[_Job]" = queue.Queue()
        self._lock = threading.Lock()
        self._status: dict[int, dict] = {}
        self._active: int | None = None
        self._worker: threading.Thread | None = None
        self._pw = None
        self._context = None
        # live streaming state (keyed by app_id so multiple open panels stay isolated)
        self._input_qs: dict[int, "queue.Queue[dict]"] = {}
        self._frame_sinks: dict[int, Callable[[bytes], None]] = {}
        self._stop = threading.Event()

    # --- called from request / websocket threads -----------------------
    def enqueue(self, app_id: int, apply_url: str, answers: list, resume_path: str | None) -> None:
        with self._lock:
            ahead = self._q.qsize() + (1 if self._active is not None else 0)
            self._status[app_id] = {
                "stage": "queued", "queue_pos": ahead, "filled": 0, "missed": [], "done": False,
            }
        self._q.put(_Job(app_id, apply_url, answers or [], resume_path))
        self._ensure_worker()

    def snapshot(self, app_id: int) -> dict:
        with self._lock:
            s = self._status.get(app_id)
            return dict(s) if s else {}

    def active_id(self) -> int | None:
        with self._lock:
            return self._active

    def set_frame_sink(self, app_id: int, cb: Callable[[bytes], None]) -> None:
        self._frame_sinks[app_id] = cb

    def clear_frame_sink(self, app_id: int) -> None:
        self._frame_sinks.pop(app_id, None)

    def push_input(self, app_id: int, ev: dict) -> None:
        with self._lock:
            q = self._input_qs.get(app_id)
            if q is None:
                q = self._input_qs[app_id] = queue.Queue()
        q.put(ev)

    def stop_live(self, app_id: int) -> None:
        # Only the currently-active job can be stopped (a queued panel closing
        # must not kill the job that's live).
        if self.active_id() == app_id:
            self._stop.set()

    # --- internals (worker thread) -------------------------------------
    def _set(self, app_id: int, **kw) -> None:
        with self._lock:
            self._status.setdefault(app_id, {}).update(kw)

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, daemon=True, name="assist-worker")
                self._worker.start()

    def _ensure_browser(self) -> None:
        if self._context is not None:
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            self.user_data_dir, headless=self.headless, viewport=dict(_VIEWPORT)
        )

    def _apply_input(self, page, ev: dict) -> None:
        """Forward one user input event from the streamed view to the real page."""
        t = ev.get("type")
        try:
            if t == "move":
                page.mouse.move(ev["x"], ev["y"])
            elif t == "click":
                page.mouse.click(ev["x"], ev["y"], button=ev.get("button", "left"),
                                 click_count=int(ev.get("clicks", 1)))
            elif t == "down":
                page.mouse.move(ev["x"], ev["y"])
                page.mouse.down(button=ev.get("button", "left"))
            elif t == "up":
                page.mouse.up(button=ev.get("button", "left"))
            elif t == "scroll":
                page.mouse.wheel(ev.get("dx", 0), ev.get("dy", 0))
            elif t == "text":
                page.keyboard.type(ev.get("text", ""))
            elif t == "key":
                page.keyboard.press(ev.get("key", ""))
        except Exception as exc:  # noqa: BLE001
            log.debug("input apply failed %s: %s", ev, exc)

    def _serve_live(self, page, app_id: int) -> None:
        """Stream the page (screenshots) + forward queued input until stopped."""
        if not self.wait_for_user:
            return
        with self._lock:
            q = self._input_qs.setdefault(app_id, queue.Queue())
        try:  # drain any stale input from a previous session
            while True:
                q.get_nowait()
        except queue.Empty:
            pass
        self._stop.clear()
        deadline = time.time() + self.keep_open_seconds
        while not self._stop.is_set() and not page.is_closed() and time.time() < deadline:
            try:
                while True:
                    self._apply_input(page, q.get_nowait())
            except queue.Empty:
                pass
            sink = self._frame_sinks.get(app_id)
            if sink is not None:
                try:
                    sink(page.screenshot(type="jpeg", quality=55))
                except Exception:
                    pass
            try:
                page.wait_for_timeout(self.frame_interval * 1000)
            except Exception:
                break

    def _run(self) -> None:
        while True:
            job = self._q.get()
            try:
                if job.apply_url == _SHUTDOWN:
                    self._teardown()
                    return
                self._process(job)
            except Exception as exc:  # noqa: BLE001 — never kill the worker
                log.warning("assist job failed (app %s): %s", job.app_id, exc)
                self._set(job.app_id, stage="error", done=True, error=str(exc))
                with self._lock:
                    self._active = None
            finally:
                self._q.task_done()

    def _teardown(self) -> None:
        """Close the browser + Playwright — MUST run on the worker thread."""
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._context = None
        self._pw = None

    def close(self) -> None:
        """Signal the worker to close its browser and exit (used on shutdown/tests)."""
        self._stop.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            self._q.put(_Job(app_id=-1, apply_url=_SHUTDOWN))
            worker.join(timeout=15)

    def _process(self, job: _Job) -> None:
        self._ensure_browser()
        with self._lock:
            self._active = job.app_id
        self._set(job.app_id, stage="opening", queue_pos=0)

        page, filled, missed = open_and_fill(
            self._context, job.apply_url, job.answers, job.resume_path
        )
        self._set(job.app_id, stage="live", filled=filled, missed=missed)

        self._serve_live(page, job.app_id)  # stream + forward input until the user is done

        try:
            if not page.is_closed():
                page.close()
        except Exception:
            pass
        self._set(job.app_id, stage="done", done=True)
        with self._lock:
            self._active = None


# --- module singleton + thin wrappers (mirrors app/pipeline/state.py) ----
_SESSION: AssistSession | None = None
_SESSION_LOCK = threading.Lock()


def _session() -> AssistSession:
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            s = get_settings()
            _SESSION = AssistSession(user_data_dir=s.assist_user_data_dir, headless=s.assist_headless)
        return _SESSION


def enqueue(app_id: int, apply_url: str, answers: list, resume_path: str | None) -> None:
    _session().enqueue(app_id, apply_url, answers, resume_path)


def snapshot(app_id: int) -> dict:
    return _session().snapshot(app_id)


def active_id() -> int | None:
    return _session().active_id()


def set_frame_sink(app_id: int, cb: Callable[[bytes], None]) -> None:
    _session().set_frame_sink(app_id, cb)


def clear_frame_sink(app_id: int) -> None:
    _session().clear_frame_sink(app_id)


def push_input(app_id: int, ev: dict) -> None:
    _session().push_input(app_id, ev)


def stop_live(app_id: int) -> None:
    _session().stop_live(app_id)


def shutdown() -> None:
    """Close the managed browser on app shutdown (no-op if never started)."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None:
            _SESSION.close()
            _SESSION = None
