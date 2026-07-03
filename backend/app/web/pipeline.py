"""Pipeline page: background cycle runner + live status/logs."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.controls import get_or_create_control
from app.db import engine
from app.models import Run
from app.pipeline.orchestrator import start_pipeline_run
from app.pipeline.state import PIPELINE_STATE

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


@router.get("/pipeline", response_class=HTMLResponse)
def pipeline_view(request: Request) -> HTMLResponse:
    with Session(engine) as session:
        runs = session.exec(select(Run).order_by(Run.started_at.desc()).limit(20)).all()
        control = get_or_create_control(session)
    return templates.TemplateResponse(
        request, "pipeline.html", {"runs": runs, "control": control, "s": PIPELINE_STATE.snapshot()}
    )


@router.post("/pipeline/run", response_class=HTMLResponse)
def pipeline_run(request: Request) -> HTMLResponse:
    start_pipeline_run()  # non-blocking; no-op if a run is already in progress
    return templates.TemplateResponse(request, "pipeline_status.html", {"s": PIPELINE_STATE.snapshot()})


@router.get("/pipeline/status", response_class=HTMLResponse)
def pipeline_status(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "pipeline_status.html", {"s": PIPELINE_STATE.snapshot()})
