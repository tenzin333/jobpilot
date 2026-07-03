"""Matches: job cards with match scores + click-to-apply-in-background.

Clicking Apply schedules apply_one (tailor -> submit) as a background task and
returns immediately, so the user can apply to the next card while this runs.
The card's status cell polls itself via HTMX until the apply reaches a terminal
state.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.db import engine
from app.models import Application, ApplicationStatus, Job
from app.pipeline.apply import apply_one
from app.pipeline.state import apply_progress, apply_start

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

# Statuses where the apply is finished (stop polling).
_TERMINAL = {
    ApplicationStatus.submitted.value,
    ApplicationStatus.queued.value,
    ApplicationStatus.needs_human.value,
    ApplicationStatus.failed.value,
}
_LABELS = {
    "submitted": "✅ Submitted",
    "queued": "✅ Filled (dry-run)",
    "needs_human": "⚠ Needs you",
    "failed": "✗ Failed",
    "ranked": "Ranked",
    "tailored": "Tailored",
}
# In-progress stage labels (from the per-app tracker).
_STAGE_LABELS = {
    "starting": "Starting…",
    "tailoring": "Tailoring résumé…",
    "submitting": "Submitting…",
}
# Final apply_one result keys -> friendly badge (covers retryable/blocked cases).
_RESULT_LABELS = {
    "submitted": "✅ Submitted",
    "dry_run": "✅ Filled (dry-run)",
    "needs_human": "⚠ Needs you",
    "failed": "✗ Failed",
    "skipped_cap": "Daily cap reached",
    "blocked": "Blocked (kill switch)",
    "retry_tailor": "✗ Rate-limited — click Retry",
    "no_profile": "No profile",
    "missing": "✗ Error",
}


def _badge(app_id: int, label: str) -> str:
    return f'<span id="cs-{app_id}" class="badge">{label}</span>'


def _polling(app_id: int, label: str) -> str:
    return (
        f'<span id="cs-{app_id}" class="badge" '
        f'hx-get="/matches/{app_id}/status" hx-trigger="every 2s" hx-swap="outerHTML">'
        f'<span class="spinner"></span> {label}</span>'
    )


def status_fragment(app_id: int, status: str) -> str:
    """Status cell driven by the per-app tracker (stage + elapsed), falling back
    to the DB status. Polls while the apply is running, stops when it finishes."""
    prog = apply_progress(app_id)
    if prog:
        if not prog.get("done"):
            stage = _STAGE_LABELS.get(prog.get("stage", ""), "Working…")
            return _polling(app_id, f"{stage} ({prog.get('elapsed', 0)}s)")
        result = prog.get("result") or status
        return _badge(app_id, _RESULT_LABELS.get(result, _LABELS.get(result, result)))

    # No active apply (no tracker entry, e.g. after a restart): show the
    # persisted status as a static badge — don't poll forever.
    return _badge(app_id, _LABELS.get(status, status))


@router.get("/matches", response_class=HTMLResponse)
def matches(request: Request) -> HTMLResponse:
    with Session(engine) as session:
        rows = session.exec(
            select(Application, Job)
            .join(Job)
            .where(Application.match_score.is_not(None))
            .order_by(Application.match_score.desc())
        ).all()
    return templates.TemplateResponse(
        request, "matches.html", {"rows": rows, "labels": _LABELS, "terminal": _TERMINAL}
    )


@router.post("/matches/{app_id}/apply", response_class=HTMLResponse)
def apply(app_id: int, background_tasks: BackgroundTasks) -> HTMLResponse:
    with Session(engine) as session:
        app = session.get(Application, app_id)
        if app is None:
            raise HTTPException(status_code=404)
        status = app.status
    # Mark started now (so the returned fragment polls), then run in background.
    apply_start(app_id)
    background_tasks.add_task(apply_one, app_id)
    return HTMLResponse(status_fragment(app_id, status))


@router.get("/matches/{app_id}/status", response_class=HTMLResponse)
def status(app_id: int) -> HTMLResponse:
    with Session(engine) as session:
        app = session.get(Application, app_id)
        if app is None:
            raise HTTPException(status_code=404)
        return HTMLResponse(status_fragment(app_id, app.status))


@router.post("/matches/{app_id}/retry", response_class=HTMLResponse)
def retry(app_id: int, background_tasks: BackgroundTasks) -> HTMLResponse:
    """Re-attempt a failed application (re-tailor if needed, then submit)."""
    with Session(engine) as session:
        app = session.get(Application, app_id)
        if app is None:
            raise HTTPException(status_code=404)
        if app.status in (ApplicationStatus.failed.value, ApplicationStatus.needs_human.value):
            # Resume from where it stopped: re-submit if already tailored, else re-tailor.
            app.status = (
                ApplicationStatus.tailored.value if app.resume_path
                else ApplicationStatus.ranked.value
            )
            app.needs_human_reason = ""
            session.add(app)
            session.commit()
        status_value = app.status
    apply_start(app_id)
    background_tasks.add_task(apply_one, app_id)
    return HTMLResponse(status_fragment(app_id, status_value))
