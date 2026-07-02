"""Apply to a single job in the background (tailor -> submit).

Used by the per-card "Apply" button: the HTTP request schedules apply_one and
returns immediately, so the user can move to the next card while this runs.
Designed to be safe to run concurrently (per-call session; SQLite WAL).
"""
from __future__ import annotations

import logging

from sqlmodel import Session, select

from app.config import get_preferences
from app.controls import effective_settings
from app.db import engine
from app.models import Application, ApplicationStatus, Job, Profile
from app.pipeline.state import apply_finish, apply_set_stage, apply_start
from app.pipeline.submit import submit_one
from app.pipeline.tailor import tailor_application

log = logging.getLogger(__name__)


def apply_one(app_id: int) -> str:
    """Tailor (if needed) then submit one application. Returns the outcome key.

    Reports progress to the per-app tracker (state.apply_*) so the UI can show
    the current stage + elapsed time and stop polling when finished.
    """
    apply_start(app_id)
    try:
        result = _apply_one(app_id)
    except Exception as exc:  # noqa: BLE001 — never leave the tracker hanging
        log.warning("apply_one crashed (app %s): %s", app_id, exc)
        result = "failed"
    apply_finish(app_id, result)
    log.info("apply_one finished (app %s): %s", app_id, result)
    return result


def _apply_one(app_id: int) -> str:
    with Session(engine) as session:
        app = session.get(Application, app_id)
        if app is None:
            return "missing"
        profile = session.exec(select(Profile)).first()
        if profile is None:
            return "no_profile"
        job = session.get(Job, app.job_id)
        if job is None:
            return "missing"

        # Only act on not-yet-processed applications (prevents double submit).
        if app.status not in (ApplicationStatus.ranked.value, ApplicationStatus.tailored.value):
            return f"already:{app.status}"

        prefs = get_preferences()
        settings = effective_settings(session)

        # Tailor if not already done (generates resume + cover letter PDFs).
        if app.status == ApplicationStatus.ranked.value:
            apply_set_stage(app_id, "tailoring")
            try:
                tailor_application(session, app, job, profile, settings)
            except Exception as exc:  # noqa: BLE001
                # Leave the app 'ranked' (retryable) — a transient rate-limit
                # shouldn't permanently fail it or exclude it from the next run.
                session.rollback()
                log.warning("apply_one tailor failed (app %s); left ranked for retry: %s", app_id, exc)
                return "retry_tailor"

        apply_set_stage(app_id, "submitting")
        try:
            return submit_one(session, app, profile, prefs, settings)
        except Exception as exc:  # noqa: BLE001
            log.warning("apply_one submit failed (app %s): %s", app_id, exc)
            app.status = ApplicationStatus.failed.value
            app.events = [*app.events, {"event": "failed", "error": f"submit: {exc}"}]
            session.add(app)
            session.commit()
            return "failed"
