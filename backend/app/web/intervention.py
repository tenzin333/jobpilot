"""Intervention queue: applications that need the user (essay/video/captcha/etc.).

For schema-driven ATSes (Ashby/Greenhouse) the "Auto-fill & finish" action opens
a VISIBLE local browser, fills every planned answer + uploads the résumé, and
leaves the window for the user to solve the captcha and click Submit. We never
auto-solve captchas or auto-click submit.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.config import Settings
from app.db import engine
from app.models import Application, ApplicationStatus, Job, Profile
from app.submit import assist_session

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
log = logging.getLogger("intervention")


def _answers_for(app: Application, job: Job, profile: Profile | None) -> list[dict]:
    """Planned answers: prefer the stored preview, else re-plan for Ashby."""
    for ev in reversed(app.events or []):
        if ev.get("event") == "application_preview":
            return (ev.get("preview") or {}).get("answers", []) or []
    if profile is not None and job.ats_type == "ashby":
        try:
            from app.submit import ashby_api

            _, preview = ashby_api.build_and_submit(
                job, profile, Settings(dry_run=True), resume_path=app.resume_path
            )
            return preview.get("answers", []) or []
        except Exception as exc:  # noqa: BLE001
            log.warning("re-plan for assist failed (app %s): %s", app.id, exc)
    return []


def _assist_status_html(app_id: int) -> str:
    """HTMX status badge for the co-browse session: polls while active, static when done."""
    s = assist_session.snapshot(app_id)
    stage = s.get("stage")
    terminal = False
    if stage == "queued":
        pos = s.get("queue_pos", 0)
        text = f"Queued #{pos} — starts when the current one finishes." if pos else "Starting…"
    elif stage == "opening":
        text = "Opening & auto-filling…"
    elif stage == "live":
        missed = s.get("missed") or []
        text = f"Live — {s.get('filled', 0)} field(s) filled"
        if missed:
            text += "; fill manually: " + ", ".join(missed)
    elif stage == "error":
        text, terminal = "⚠ Couldn't start — use “open blank ↗”.", True
    elif stage == "done":
        text, terminal = "Session closed.", True
    else:
        text, terminal = "connecting…", False

    if terminal:
        return f'<span id="as-{app_id}" class="badge">{text}</span>'
    return (
        f'<span id="as-{app_id}" class="badge" '
        f'hx-get="/intervention/{app_id}/assist-status" hx-trigger="every 2s" hx-swap="outerHTML">'
        f'<span class="spinner"></span> {text}</span>'
    )


@router.get("/intervention", response_class=HTMLResponse)
def intervention_list(request: Request) -> HTMLResponse:
    with Session(engine) as session:
        rows = session.exec(
            select(Application, Job).join(Job).where(
                Application.status == ApplicationStatus.needs_human.value
            )
        ).all()
    return templates.TemplateResponse(request, "intervention.html", {"rows": rows})


@router.get("/intervention/{app_id}/live", response_class=HTMLResponse)
def intervention_live(request: Request, app_id: int) -> HTMLResponse:
    """Enqueue the app for the managed browser and return the in-page live panel."""
    with Session(engine) as session:
        app = session.get(Application, app_id)
        if app is None:
            raise HTTPException(status_code=404)
        job = session.get(Job, app.job_id)
        profile = session.exec(select(Profile)).first()
        apply_url = job.apply_url if job else ""
        answers = _answers_for(app, job, profile) if job else []
        resume_path = app.resume_path
    if not apply_url:
        return HTMLResponse('<span class="badge">No apply URL for this job.</span>')
    # Only enqueue if not already active/queued for this app (avoid double-add).
    stage = assist_session.snapshot(app_id).get("stage")
    if stage not in ("queued", "opening", "live"):
        assist_session.enqueue(app_id, apply_url, answers, resume_path)
    return templates.TemplateResponse(request, "_assist_live.html", {"app_id": app_id})


@router.get("/intervention/{app_id}/assist-status", response_class=HTMLResponse)
def intervention_assist_status(app_id: int) -> HTMLResponse:
    return HTMLResponse(_assist_status_html(app_id))


@router.post("/intervention/{app_id}/done")
def intervention_done(app_id: int) -> RedirectResponse:
    """User completed the application manually -> mark submitted."""
    with Session(engine) as session:
        app = session.get(Application, app_id)
        if app is None:
            raise HTTPException(status_code=404)
        app.status = ApplicationStatus.submitted.value
        app.submitted_at = datetime.now(timezone.utc)
        app.events = [*app.events, {"event": "manually_submitted", "at": app.submitted_at.isoformat()}]
        session.add(app)
        session.commit()
    return RedirectResponse(url="/intervention", status_code=303)
