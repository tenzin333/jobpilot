"""Settings page: edit runtime controls (autonomy, cap, threshold, kill switch)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app.controls import get_or_create_control, update_control
from app.db import engine

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


@router.get("/settings", response_class=HTMLResponse)
def settings_get(request: Request) -> HTMLResponse:
    with Session(engine) as session:
        control = get_or_create_control(session)
    return templates.TemplateResponse(request, "settings.html", {"control": control})


@router.post("/settings")
def settings_post(
    dry_run: bool = Form(False),
    submit_kill_switch: bool = Form(False),
    scheduler_enabled: bool = Form(False),
    daily_submit_cap: int = Form(40),
    match_threshold: int = Form(70),
    cycle_interval_minutes: int = Form(60),
) -> RedirectResponse:
    with Session(engine) as session:
        update_control(
            session,
            dry_run=dry_run,
            submit_kill_switch=submit_kill_switch,
            scheduler_enabled=scheduler_enabled,
            daily_submit_cap=daily_submit_cap,
            match_threshold=match_threshold,
            cycle_interval_minutes=cycle_interval_minutes,
        )
    return RedirectResponse(url="/settings", status_code=303)
