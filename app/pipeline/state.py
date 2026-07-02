"""Thread-safe, in-memory pipeline progress state for live UI feedback.

A single process-wide instance tracks the current cycle's stage, counts, and a
capped log buffer. The orchestrator updates it as it runs (in a background
thread); the dashboard polls a snapshot via HTMX.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

_MAX_LOGS = 60


class PipelineState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.stage = "idle"
        self.stats: dict[str, int] = {}
        self.logs: list[str] = []
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.error: str | None = None

    def start(self) -> bool:
        """Mark a run as started. Returns False if one is already running."""
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.stage = "starting"
            self.stats = {}
            self.logs = []
            self.error = None
            self.started_at = datetime.now(timezone.utc)
            self.finished_at = None
        self.log("Pipeline run started")
        return True

    def set_stage(self, stage: str) -> None:
        with self._lock:
            self.stage = stage
        self.log(stage)

    def update_stats(self, **kw: int) -> None:
        with self._lock:
            self.stats.update(kw)

    def log(self, message: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        with self._lock:
            self.logs.append(f"{ts}  {message}")
            if len(self.logs) > _MAX_LOGS:
                self.logs = self.logs[-_MAX_LOGS:]

    def finish(self, error: str | None = None) -> None:
        with self._lock:
            self.running = False
            self.stage = "error" if error else "done"
            self.error = error
            self.finished_at = datetime.now(timezone.utc)
        self.log(f"Pipeline run failed: {error}" if error else "Pipeline run finished")

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "stage": self.stage,
                "stats": dict(self.stats),
                "logs": list(self.logs),
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


PIPELINE_STATE = PipelineState()


# --- per-application apply progress (for the card / Applications-row poller) ---
_apply_lock = threading.Lock()
_apply_progress: dict[int, dict] = {}


def apply_start(app_id: int) -> None:
    with _apply_lock:
        _apply_progress[app_id] = {"stage": "starting", "started": time.monotonic(), "done": False, "result": None}


def apply_set_stage(app_id: int, stage: str) -> None:
    with _apply_lock:
        if app_id in _apply_progress:
            _apply_progress[app_id]["stage"] = stage


def apply_finish(app_id: int, result: str) -> None:
    with _apply_lock:
        p = _apply_progress.get(app_id)
        if p is not None:
            p["done"] = True
            p["result"] = result
            p["finished"] = time.monotonic()


def apply_progress(app_id: int) -> dict:
    """Snapshot of one application's apply progress, with computed 'elapsed' seconds."""
    with _apply_lock:
        p = _apply_progress.get(app_id)
        if not p:
            return {}
        snap = dict(p)
    snap["elapsed"] = int((snap.get("finished") or time.monotonic()) - snap["started"])
    return snap
