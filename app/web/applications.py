"""Applications list view + manual ranking trigger."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.config import get_preferences
from app.controls import effective_settings
from app.db import engine
from app.llm.ranking import rank_jobs
from app.models import Application, Job, Profile
from app.pipeline.submit import submit_tailored
from app.pipeline.tailor import tailor_ranked

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def _error_of(app: Application) -> str:
    """Human-readable failure reason for a failed/needs-human application."""
    if app.status == "needs_human":
        return app.needs_human_reason or ""
    if app.status == "failed":
        for ev in reversed(app.events or []):
            if ev.get("event") == "failed":
                return ev.get("error", "")
    return ""


@router.get("/applications", response_class=HTMLResponse)
def applications_list(request: Request) -> HTMLResponse:
    with Session(engine) as session:
        rows = session.exec(
            select(Application, Job).join(Job).order_by(Application.match_score.desc())
        ).all()
        settings = effective_settings(session)
        errors = {app.id: _error_of(app) for app, _ in rows}
    return templates.TemplateResponse(
        request, "applications.html",
        {"rows": rows, "errors": errors,
         "dry_run": settings.dry_run, "kill_switch": settings.submit_kill_switch},
    )


@router.post("/applications/rank")
def applications_rank() -> RedirectResponse:
    prefs = get_preferences()
    with Session(engine) as session:
        profile = session.exec(select(Profile)).first()
        if profile is not None:
            rank_jobs(session, profile, prefs)
    return RedirectResponse(url="/applications", status_code=303)


@router.post("/applications/tailor")
def applications_tailor() -> RedirectResponse:
    with Session(engine) as session:
        settings = effective_settings(session)
        profile = session.exec(select(Profile)).first()
        if profile is not None:
            tailor_ranked(session, profile, settings)
    return RedirectResponse(url="/applications", status_code=303)


@router.post("/applications/submit")
def applications_submit() -> RedirectResponse:
    prefs = get_preferences()
    with Session(engine) as session:
        settings = effective_settings(session)
        profile = session.exec(select(Profile)).first()
        if profile is not None:
            submit_tailored(session, profile, prefs, settings)
    return RedirectResponse(url="/applications", status_code=303)


@router.get("/applications/{app_id}/{artifact}")
def download_artifact(app_id: int, artifact: str) -> FileResponse:
    if artifact not in ("resume", "cover_letter"):
        raise HTTPException(status_code=404)
    with Session(engine) as session:
        app = session.get(Application, app_id)
        if app is None:
            raise HTTPException(status_code=404)
        path = app.resume_path if artifact == "resume" else app.cover_letter_path
    if not path:
        raise HTTPException(status_code=404, detail="Artifact not generated yet")
    return FileResponse(path, media_type="application/pdf", filename=f"{artifact}_{app_id}.pdf")
