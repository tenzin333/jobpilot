"""APScheduler integration for the continuous orchestrator cycle.

A single interval job runs the pipeline; it checks the runtime `scheduler_enabled`
control each fire, so the dashboard can pause/resume without a restart. The
interval itself is read at startup (changing it applies on next restart).
"""
from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlmodel import Session

from app.config import get_settings
from app.controls import get_or_create_control
from app.db import engine
from app.pipeline.orchestrator import start_pipeline_run
from app.tracking.reporting import build_summary, send_summary_email

_scheduler: AsyncIOScheduler | None = None


async def _job() -> None:
    with Session(engine) as session:
        control = get_or_create_control(session)
        if not control.scheduler_enabled:
            return
    # Run in a background thread so the cycle never blocks the event loop.
    start_pipeline_run()


def _summary_job() -> None:
    with Session(engine) as session:
        summary = build_summary(session)
    send_summary_email(get_settings(), summary)


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    with Session(engine) as session:
        interval = get_or_create_control(session).cycle_interval_minutes
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_job, IntervalTrigger(minutes=max(interval, 1)), id="cycle", replace_existing=True)
    # Daily summary email at 07:00 server-local time (no-op if SMTP unconfigured).
    _scheduler.add_job(_summary_job, CronTrigger(hour=7, minute=0), id="daily_summary", replace_existing=True)
    _scheduler.start()
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
