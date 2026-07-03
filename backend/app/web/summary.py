"""Daily summary page + manual 'email summary now' trigger."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app.config import get_settings
from app.db import engine
from app.tracking.reporting import build_summary, send_summary_email

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


@router.get("/summary", response_class=HTMLResponse)
def summary_view(request: Request) -> HTMLResponse:
    with Session(engine) as session:
        summary = build_summary(session)
    return templates.TemplateResponse(request, "summary.html", {"s": summary})


@router.post("/summary/email")
def summary_email() -> RedirectResponse:
    with Session(engine) as session:
        summary = build_summary(session)
    send_summary_email(get_settings(), summary)
    return RedirectResponse(url="/summary", status_code=303)
