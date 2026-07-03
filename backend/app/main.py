"""FastAPI application: serves the dashboard and (later) hosts the orchestrator.

Phase 0 provides: startup DB init, /health, static files, and the dashboard page.
Later phases mount additional routers (setup, jobs, applications, intervention,
settings) and start the APScheduler orchestrator on startup.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

# Surface app INFO logs (setup, discovery, resume parsing) on the console.
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select

from app.config import get_settings
from app.db import engine, init_db
from app.models import Application, ApplicationStatus, Job, Profile

WEB_DIR = Path(__file__).resolve().parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Automatic scheduler is intentionally disabled — runs happen only via the
    # manual "Run one cycle" button and the per-job Apply action.
    yield
    # Close the managed assist browser (if one was opened) on shutdown.
    from app.submit.assist_session import shutdown as assist_shutdown

    assist_shutdown()


app = FastAPI(title="Job Applier Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

# Serve the built React (Vite) SPA at /ui when it has been built (frontend/dist).
# In development the SPA runs from the Vite dev server (npm run dev), which proxies
# /api to this backend — so this mount is only used for production builds.
# __file__ is backend/app/main.py, so parents[2] is the repo root that holds frontend/.
_SPA_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _SPA_DIST.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_SPA_DIST), html=True), name="spa")

from app.web.setup import router as setup_router  # noqa: E402
from app.web.jobs import router as jobs_router  # noqa: E402
from app.web.applications import router as applications_router  # noqa: E402
from app.web.intervention import router as intervention_router  # noqa: E402
from app.web.pipeline import router as pipeline_router  # noqa: E402
from app.web.settings import router as settings_router  # noqa: E402
from app.web.summary import router as summary_router  # noqa: E402
from app.web.matches import router as matches_router  # noqa: E402
from app.web.assist_ws import router as assist_ws_router  # noqa: E402
from app.web.api import router as api_router  # noqa: E402

app.include_router(api_router)
app.include_router(setup_router)
app.include_router(jobs_router)
app.include_router(applications_router)
app.include_router(intervention_router)
app.include_router(pipeline_router)
app.include_router(settings_router)
app.include_router(summary_router)
app.include_router(matches_router)
app.include_router(assist_ws_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _count(session: Session, status: str | None = None) -> int:
    stmt = select(func.count()).select_from(Application)
    if status is not None:
        stmt = stmt.where(Application.status == status)
    return session.exec(stmt).one()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    from app.pipeline.state import PIPELINE_STATE

    settings = get_settings()
    with Session(engine) as session:
        jobs = session.exec(select(func.count()).select_from(Job)).one()
        stats = {
            "jobs": jobs,
            "ranked": _count(session, ApplicationStatus.ranked.value),
            "tailored": _count(session, ApplicationStatus.tailored.value),
            "queued": _count(session, ApplicationStatus.queued.value),
            "submitted": _count(session, ApplicationStatus.submitted.value),
            "needs_human": _count(session, ApplicationStatus.needs_human.value),
            "failed": _count(session, ApplicationStatus.failed.value),
        }
        profile = session.exec(select(Profile)).first()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": stats,
            "settings": settings,
            "profile": profile,
            "pipeline": PIPELINE_STATE.snapshot(),
        },
    )
