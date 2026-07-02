"""Submission orchestration: kill switch, autonomy, daily cap, result mapping.

Selects tailored applications and submits via the per-ATS adapter. Honors:
- global kill switch (no submissions at all),
- per-source autonomy flag (off -> routed to needs_human),
- daily submit cap,
- DRY_RUN (adapters fill but never final-click; status stays 'queued').
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, func, select

from app.config import Preferences, Settings
from app.models import Application, ApplicationStatus, Job, Profile
from app.submit.base import DryRunFilled, Failed, NeedsHuman, Submitted, SubmitAdapter
from app.submit.greenhouse_submit import GreenhouseSubmitAdapter
from app.submit.lever_submit import LeverSubmitAdapter
from app.models import AtsType

ADAPTERS: dict[str, SubmitAdapter] = {
    AtsType.greenhouse.value: GreenhouseSubmitAdapter(),
    AtsType.lever.value: LeverSubmitAdapter(),
}


def _submitted_today(session: Session) -> int:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return session.exec(
        select(func.count()).select_from(Application).where(
            Application.status == ApplicationStatus.submitted.value,
            Application.submitted_at >= start,
        )
    ).one()


def _record(app: Application, status: str, event: dict, *, submitted: bool = False) -> None:
    app.status = status
    app.updated_at = datetime.now(timezone.utc)
    if submitted:
        app.submitted_at = datetime.now(timezone.utc)
    app.events = [*app.events, {**event, "at": datetime.now(timezone.utc).isoformat()}]


def submit_one(
    session: Session,
    app: Application,
    profile: Profile,
    prefs: Preferences,
    settings: Settings,
    *,
    remaining_cap: int | None = None,
) -> str:
    """Submit a single tailored application. Returns the outcome key.

    Enforces kill switch, per-source autonomy, daily cap, and DRY_RUN. Outcome is
    one of: submitted | dry_run | needs_human | failed | skipped_cap | blocked.
    """
    if settings.submit_kill_switch:
        return "blocked"

    job = session.get(Job, app.job_id)
    if job is None:
        _record(app, ApplicationStatus.failed.value, {"event": "failed", "error": "job missing"})
        session.add(app)
        session.commit()
        return "failed"

    is_greenhouse = job.ats_type == AtsType.greenhouse.value
    is_ashby = job.ats_type == AtsType.ashby.value
    adapter = ADAPTERS.get(job.ats_type)
    if not is_greenhouse and not is_ashby and adapter is None:
        _record(app, ApplicationStatus.needs_human.value, {"event": "needs_human", "reason": "unsupported ATS"})
        app.needs_human_reason = "unsupported ATS"
        session.add(app)
        session.commit()
        return "needs_human"

    source_cfg = prefs.sources.get(job.source)
    if source_cfg is None or not source_cfg.autonomy:
        _record(app, ApplicationStatus.needs_human.value, {"event": "needs_human", "reason": "source autonomy disabled"})
        app.needs_human_reason = "source autonomy disabled"
        session.add(app)
        session.commit()
        return "needs_human"

    cap = remaining_cap if remaining_cap is not None else settings.daily_submit_cap - _submitted_today(session)
    if not settings.dry_run and cap <= 0:
        return "skipped_cap"

    # Greenhouse and Ashby use schema-driven submitters (answer every field via
    # the answer bank + LLM); other sources use the browser adapter.
    preview: dict | None = None
    if is_greenhouse:
        from app.submit import greenhouse_api

        result, preview = greenhouse_api.build_and_submit(job, profile, settings)
    elif is_ashby:
        from app.submit import ashby_api

        result, preview = ashby_api.build_and_submit(job, profile, settings, resume_path=app.resume_path)
    else:
        result = adapter.submit(
            apply_url=job.apply_url, profile=profile, resume_path=app.resume_path, dry_run=settings.dry_run
        )
    if preview:
        app.events = [*app.events, {"event": "application_preview", "preview": preview}]

    if isinstance(result, Submitted):
        _record(app, ApplicationStatus.submitted.value, {"event": "submitted"}, submitted=True)
        outcome = "submitted"
    elif isinstance(result, DryRunFilled):
        _record(app, ApplicationStatus.queued.value, {"event": "dry_run_filled"})
        outcome = "dry_run"
    elif isinstance(result, NeedsHuman):
        _record(app, ApplicationStatus.needs_human.value, {"event": "needs_human", "reason": result.reason})
        app.needs_human_reason = result.reason
        outcome = "needs_human"
    else:  # Failed
        _record(app, ApplicationStatus.failed.value, {"event": "failed", "error": result.error})
        outcome = "failed"

    session.add(app)
    session.commit()
    return outcome


def submit_tailored(
    session: Session, profile: Profile, prefs: Preferences, settings: Settings
) -> dict[str, int]:
    stats = {"submitted": 0, "dry_run": 0, "needs_human": 0, "failed": 0, "skipped_cap": 0}

    if settings.submit_kill_switch:
        return stats

    remaining = settings.daily_submit_cap - _submitted_today(session)

    apps = session.exec(
        select(Application).where(Application.status == ApplicationStatus.tailored.value)
    ).all()

    for app in apps:
        outcome = submit_one(session, app, profile, prefs, settings, remaining_cap=remaining)
        if outcome in stats:
            stats[outcome] += 1
        if outcome == "submitted":
            remaining -= 1

    return stats
