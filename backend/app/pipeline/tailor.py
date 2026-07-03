"""Orchestrate tailoring: for ranked apps above threshold, generate + render PDFs."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.config import Settings
from app.llm.tailoring import tailor
from app.models import Application, ApplicationStatus, Job
from app.models import Profile
from app.resume.render import render_cover_letter_pdf, render_resume_pdf

log = logging.getLogger("tailor")


def tailor_application(
    session: Session, application: Application, job: Job, profile: Profile, settings: Settings
) -> list[str]:
    """Generate + render artifacts for one application. Returns removed (fabricated) skills."""
    tailored, removed = tailor(job, profile)

    base = settings.artifacts_dir / f"app_{application.id}"
    resume_path = render_resume_pdf(tailored, profile, base / "resume.pdf")
    cover_path = render_cover_letter_pdf(tailored, job, profile, base / "cover_letter.pdf")

    application.resume_path = str(resume_path)
    application.cover_letter_path = str(cover_path)
    application.status = ApplicationStatus.tailored.value
    application.updated_at = datetime.now(timezone.utc)
    event = {"event": "tailored", "at": datetime.now(timezone.utc).isoformat()}
    if removed:
        event["removed_fabricated_skills"] = removed
    application.events = [*application.events, event]
    session.add(application)
    session.commit()
    return removed


def tailor_ranked(session: Session, profile: Profile, settings: Settings) -> dict[str, int]:
    """Tailor all ranked applications scoring at/above the match threshold."""
    # Include previously-failed apps so transient (rate-limit) failures get retried.
    apps = session.exec(
        select(Application).where(
            Application.status.in_([ApplicationStatus.ranked.value, ApplicationStatus.failed.value]),
            Application.match_score >= settings.match_threshold,
        )
    ).all()

    tailored_count = 0
    for app in apps:
        job = session.get(Job, app.job_id)
        if job is None:
            continue
        try:
            tailor_application(session, app, job, profile, settings)
            tailored_count += 1
        except Exception as exc:  # noqa: BLE001
            # Transient (e.g. rate-limit) failure: leave the app 'ranked' so the
            # next run retries it, and keep tailoring the rest of the batch.
            session.rollback()
            log.warning("Tailoring failed for app %s (%s); will retry next run: %s",
                        app.id, job.title, exc)
    return {"tailored": tailored_count}
