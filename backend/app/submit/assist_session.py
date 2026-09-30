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
from app.submit.ashby_assist import (
    ApplicationAutomationState,
    advance_application_step,
    application_form_ready,
    application_state_signature,
    login_gate,
    open_and_fill,
)

log = logging.getLogger("assist")

_VIEWPORT = {"width": 1280, "height": 800}
_SHUTDOWN = "__shutdown__"  # sentinel apply_url that tells the worker to tear down

_SCROLL_METRICS_JS = """
() => {
  const root = document.scrollingElement || document.documentElement;
  const rootStyle = getComputedStyle(root);
  const rootCanScroll = root.scrollHeight > root.clientHeight + 1
    && rootStyle.overflowY !== 'hidden';
  let target = root;
  if (!rootCanScroll) {
    const candidates = Array.from(document.querySelectorAll('*')).filter((element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return element.scrollHeight > element.clientHeight + 1
        && ['auto', 'scroll', 'overlay'].includes(style.overflowY)
        && rect.width > 120 && rect.height > 120;
    });
    candidates.sort((a, b) => b.clientHeight * b.clientWidth - a.clientHeight * a.clientWidth);
    if (candidates.length) target = candidates[0];
  }
  return {
    y: Math.round(target === root ? (window.scrollY || root.scrollTop || 0) : target.scrollTop),
    max: Math.max(0, Math.round(target.scrollHeight - target.clientHeight)),
  };
}
"""

_SCROLL_TO_JS = """
(requestedY) => {
  const root = document.scrollingElement || document.documentElement;
  const rootStyle = getComputedStyle(root);
  const rootCanScroll = root.scrollHeight > root.clientHeight + 1
    && rootStyle.overflowY !== 'hidden';
  let target = root;
  if (!rootCanScroll) {
    const candidates = Array.from(document.querySelectorAll('*')).filter((element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return element.scrollHeight > element.clientHeight + 1
        && ['auto', 'scroll', 'overlay'].includes(style.overflowY)
        && rect.width > 120 && rect.height > 120;
    });
    candidates.sort((a, b) => b.clientHeight * b.clientWidth - a.clientHeight * a.clientWidth);
    if (candidates.length) target = candidates[0];
  }
  const maximum = Math.max(0, target.scrollHeight - target.clientHeight);
  target.scrollTop = Math.max(0, Math.min(maximum, Number(requestedY) || 0));
}
"""

# Injected before any page script to hide the most common automation signals so
# the ATS anti-bot (reCAPTCHA Enterprise + fingerprinting) sees an authentic browser.
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
const _q = window.navigator.permissions && window.navigator.permissions.query;
if (_q) {
  window.navigator.permissions.query = (p) =>
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : _q(p);
}
"""


@dataclass
class _Job:
    app_id: int
    apply_url: str
    answers: list = field(default_factory=list)
    resume_path: str | None = None
    task: Callable | None = None


class AssistSession:
    def __init__(self, *, user_data_dir: str, headless: bool = True,
                 wait_for_user: bool = True, keep_open_seconds: int = 1800,
                 frame_interval: float = 0.1, channel: str = "chrome",
                 hide_window: bool = True):
        self.user_data_dir = user_data_dir
        self.headless = headless
        self.channel = channel
        self.hide_window = hide_window
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
        self._frame_sinks: dict[int | str, Callable[[bytes], None]] = {}
        self._latest_frames: dict[int | str, bytes] = {}
        self._stop = threading.Event()
        self._closing = threading.Event()

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

    def enqueue_task(self, app_id: int, task: Callable) -> None:
        """Dispatch caller-owned work on the same serial browser thread."""
        self._q.put(_Job(app_id, "", task=task))
        self._ensure_worker()

    def emit_frame(self, key: str, page, *, full_page: bool = False) -> None:
        self._publish_frame(
            key,
            page.screenshot(type="jpeg", quality=60, full_page=full_page),
        )

    def _publish_frame(self, key: int | str, frame: bytes) -> None:
        with self._lock:
            # Keep a bounded replay buffer so a panel that connects after an
            # early action immediately receives the most recent browser state.
            self._latest_frames.pop(key, None)
            self._latest_frames[key] = frame
            while len(self._latest_frames) > 32:
                self._latest_frames.pop(next(iter(self._latest_frames)))
            sink = self._frame_sinks.get(key)
        if sink is not None:
            sink(frame)

    def active_id(self) -> int | None:
        with self._lock:
            return self._active

    def set_frame_sink(self, app_id: int | str, cb: Callable[[bytes], None]) -> None:
        with self._lock:
            self._frame_sinks[app_id] = cb
            latest = self._latest_frames.get(app_id)
        if latest is not None:
            cb(latest)

    def clear_frame_sink(self, app_id: int | str) -> None:
        with self._lock:
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

        if self._pw is None:
            self._pw = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled"]
        if not self.headless and self.hide_window:
            # Real headed Chrome (authentic fingerprint) but positioned off-screen
            # so no window is visible; we still stream it via screenshots.
            args += ["--window-position=-32000,-32000"]
        opts = dict(
            headless=self.headless,
            viewport=dict(_VIEWPORT),
            args=args,
            ignore_default_args=["--enable-automation"],
            service_workers="block",
        )
        try:  # prefer the user's installed Google Chrome (more authentic than bundled Chromium)
            self._context = self._pw.chromium.launch_persistent_context(
                self.user_data_dir, channel=self.channel, **opts)
        except Exception as exc:  # noqa: BLE001 — Chrome not installed, etc.
            log.info("assist: channel=%s unavailable (%s); using bundled Chromium", self.channel, exc)
            self._context = self._pw.chromium.launch_persistent_context(self.user_data_dir, **opts)
        self._context.on("close", self._browser_closed)
        try:
            self._context.add_init_script(_STEALTH_JS)
        except Exception:
            pass

    def _browser_closed(self, _context=None) -> None:
        # Callback runs on the browser owner thread. Reuse Playwright, relaunch
        # the persistent context for the next explicitly requested execution.
        self._context = None

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
            elif t == "scroll_to":
                page.evaluate(_SCROLL_TO_JS, ev.get("y", 0))
            elif t == "text":
                page.keyboard.type(ev.get("text", ""))
            elif t == "key":
                page.keyboard.press(ev.get("key", ""))
            elif t == "history_back":
                page.go_back(wait_until="commit", timeout=10_000)
            elif t == "history_forward":
                page.go_forward(wait_until="commit", timeout=10_000)
        except Exception as exc:  # noqa: BLE001
            # Input may contain credentials, so log only the event type.
            log.debug("input apply failed (type=%s): %s", t, exc)

    def _scroll_metrics(self, page) -> tuple[int, int]:
        """Return the active document/container's vertical position and extent."""
        try:
            metrics = page.evaluate(_SCROLL_METRICS_JS)
            return int(metrics.get("y", 0)), int(metrics.get("max", 0))
        except Exception:
            return 0, 0

    def _serve_live(
        self,
        page,
        app_id: int,
        *,
        answers: list | None = None,
        resume_path: str | None = None,
        continue_to_application: bool = False,
    ) -> None:
        """Stream input/frames and continue into the form after authentication."""
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
        active_page = page
        pending_autofill = continue_to_application
        automation_state = ApplicationAutomationState()
        filled_total = 0
        auth_clear_at: float | None = None
        handoff_signature: str | None = None
        changed_signature: str | None = None
        changed_signature_at: float | None = None
        progress_grace_until = 0.0
        last_scroll_metrics: tuple[int, int] | None = None
        next_scroll_check = 0.0
        while not self._stop.is_set() and time.time() < deadline:
            # OAuth commonly opens another page. Stream and control the newest
            # one, then fall back when it closes.
            try:
                pages = [candidate for candidate in self._context.pages if not candidate.is_closed()]
                if pages and active_page is not pages[-1]:
                    active_page = pages[-1]
                    self._publish_frame(
                        app_id, active_page.screenshot(type="jpeg", quality=55)
                    )
            except Exception:
                pages = []
            if not pages:
                break
            resume_requested = False
            try:
                while True:
                    event = q.get_nowait()
                    if event.get("type") == "resume_automation":
                        resume_requested = True
                    else:
                        self._apply_input(active_page, event)
            except queue.Empty:
                pass

            if resume_requested:
                pending_autofill = True
                auth_clear_at = time.monotonic() - 0.75
                progress_grace_until = time.monotonic() + 20.0
                handoff_signature = None
                changed_signature = None
                changed_signature_at = None
                automation_state.attempted_progress.clear()
                automation_state.attempted_apply_urls.clear()
                self._set(app_id, stage="opening", missed=[])

            if not pending_autofill and handoff_signature is not None:
                current_signature = application_state_signature(active_page)
                now = time.monotonic()
                if current_signature == handoff_signature:
                    changed_signature = None
                    changed_signature_at = None
                elif current_signature != changed_signature:
                    changed_signature = current_signature
                    changed_signature_at = now
                elif changed_signature_at is not None and now - changed_signature_at >= 0.75:
                    pending_autofill = True
                    auth_clear_at = now - 0.75
                    progress_grace_until = now + 20.0
                    handoff_signature = None
                    changed_signature = None
                    changed_signature_at = None
                    self._set(app_id, stage="opening", missed=[])

            if pending_autofill:
                # The newest page is the controlled surface. Registration and
                # OAuth links may intentionally leave a login form open in the
                # opener; that stale tab must not block the active application.
                gated = login_gate(active_page)
                if gated:
                    auth_clear_at = None
                    self._set(app_id, stage="login_required", filled=0, missed=[])
                else:
                    now = time.monotonic()
                    if auth_clear_at is None:
                        auth_clear_at = now
                        self._set(app_id, stage="opening", filled=filled_total, missed=[])
                    elif now - auth_clear_at >= 0.75:
                        self._set(app_id, stage="filling", filled=filled_total, missed=[])
                        result = advance_application_step(
                            active_page,
                            answers or [],
                            resume_path,
                            automation_state,
                            lambda current: self._publish_frame(
                                app_id,
                                current.screenshot(type="jpeg", quality=55),
                            ),
                        )
                        filled_total += result.filled
                        if result.outcome == "progressed":
                            progress_grace_until = time.monotonic() + 20.0
                            auth_clear_at = None
                            self._set(
                                app_id,
                                stage="opening",
                                filled=filled_total,
                                missed=[],
                            )
                        elif (
                            result.outcome == "needs_user"
                            and result.missed == ("Choose the next application step",)
                            and time.monotonic() < progress_grace_until
                        ):
                            # A portal may return from Continue before its next
                            # SPA step has mounted. Retry within a bounded grace
                            # period instead of turning a transient blank state
                            # into a human handoff.
                            auth_clear_at = time.monotonic()
                            self._set(app_id, stage="opening", missed=[])
                        else:
                            progress_grace_until = 0.0
                            self._set(
                                app_id,
                                stage="live",
                                filled=filled_total,
                                missed=list(result.missed),
                            )
                            pending_autofill = False
                            handoff_signature = (
                                application_state_signature(active_page)
                                if result.outcome == "needs_user"
                                else None
                            )
            now = time.monotonic()
            if now >= next_scroll_check:
                metrics = self._scroll_metrics(active_page)
                if metrics != last_scroll_metrics:
                    self._set(app_id, scroll_y=metrics[0], scroll_max=metrics[1])
                    last_scroll_metrics = metrics
                next_scroll_check = now + 0.4
            try:
                self._publish_frame(app_id, active_page.screenshot(type="jpeg", quality=55))
            except Exception:
                pass
            try:
                active_page.wait_for_timeout(self.frame_interval * 1000)
            except Exception:
                continue

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
        self._closing.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            self._q.put(_Job(app_id=-1, apply_url=_SHUTDOWN))
            worker.join(timeout=15)

    def _process(self, job: _Job) -> None:
        if job.task is not None:
            with self._lock:
                self._active = job.app_id
            try:
                job.task(self)
            finally:
                with self._lock:
                    self._active = None
            return
        self._ensure_browser()
        with self._lock:
            self._active = job.app_id
        self._set(job.app_id, stage="opening", queue_pos=0)

        page, filled, missed = open_and_fill(
            self._context,
            job.apply_url,
            job.answers,
            job.resume_path,
            lambda page: self._publish_frame(
                job.app_id,
                page.screenshot(type="jpeg", quality=55),
            ),
        )
        needs_login = login_gate(page)
        needs_application_navigation = not needs_login and not application_form_ready(page)
        if not self.wait_for_user:
            self._set(job.app_id, stage="live", filled=filled, missed=missed)
        elif needs_login:
            self._set(job.app_id, stage="login_required", filled=0, missed=[])
        elif needs_application_navigation:
            self._set(job.app_id, stage="opening", filled=0, missed=[])
        else:
            self._set(job.app_id, stage="filling", filled=0, missed=[])

        self._serve_live(
            page,
            job.app_id,
            answers=job.answers,
            resume_path=job.resume_path,
            continue_to_application=self.wait_for_user,
        )

        # Cookies remain in the persistent context; close every job tab so an
        # OAuth or off-site popup cannot leak into the next queued application.
        for current in list(self._context.pages):
            try:
                if not current.is_closed():
                    current.close()
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
            _SESSION = AssistSession(
                user_data_dir=s.assist_user_data_dir,
                # The dashboard stream is the only browser surface. A headed
                # instance would create the duplicate Chrome window the
                # embedded workspace is designed to replace.
                headless=True,
                channel=s.assist_channel, hide_window=s.assist_hide_window)
        return _SESSION


def enqueue(app_id: int, apply_url: str, answers: list, resume_path: str | None) -> None:
    _session().enqueue(app_id, apply_url, answers, resume_path)


def snapshot(app_id: int) -> dict:
    return _session().snapshot(app_id)


def active_id() -> int | None:
    return _session().active_id()


def set_frame_sink(app_id: int | str, cb: Callable[[bytes], None]) -> None:
    _session().set_frame_sink(app_id, cb)


def clear_frame_sink(app_id: int | str) -> None:
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
