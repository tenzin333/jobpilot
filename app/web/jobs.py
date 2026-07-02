"""Discover page: score cards + background discover-and-score + clear."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, delete, select

from app.db import engine
from app.models import Application, Job
from app.pipeline.orchestrator import start_discover_and_rank
from app.pipeline.state import PIPELINE_STATE
from app.web.matches import _LABELS

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def _grid_rows(session: Session):
    # Left-join each job to its ranking so cards show the match score, best first.
    return session.exec(
        select(Job, Application)
        .join(Application, isouter=True)
        .order_by(Application.match_score.desc(), Job.discovered_at.desc())
    ).all()


@router.get("/jobs", response_class=HTMLResponse)
def jobs_list(request: Request) -> HTMLResponse:
    with Session(engine) as session:
        rows = _grid_rows(session)
    return templates.TemplateResponse(
        request, "jobs.html",
        {"rows": rows, "labels": _LABELS, "running": PIPELINE_STATE.snapshot()["running"]},
    )


@router.get("/jobs/grid", response_class=HTMLResponse)
def jobs_grid(request: Request) -> HTMLResponse:
    with Session(engine) as session:
        rows = _grid_rows(session)
    return templates.TemplateResponse(
        request, "jobs_grid.html",
        {"rows": rows, "labels": _LABELS, "running": PIPELINE_STATE.snapshot()["running"]},
    )


@router.post("/jobs/discover")
def jobs_discover() -> RedirectResponse:
    # Non-blocking: discover + score in the background; cards fill in live.
    start_discover_and_rank()
    return RedirectResponse(url="/jobs", status_code=303)


@router.post("/jobs/clear")
def jobs_clear() -> RedirectResponse:
    """Delete all jobs and their applications (lets you reset a polluted pool)."""
    with Session(engine) as session:
        session.exec(delete(Application))
        session.exec(delete(Job))
        session.commit()
    return RedirectResponse(url="/jobs", status_code=303)
