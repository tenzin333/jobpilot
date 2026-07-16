"""JSON API for the React (Vite) frontend.

These endpoints mirror the data the existing HTMX pages render, but return JSON so
the SPA in ``frontend/`` can consume them. The legacy server-rendered pages remain
untouched; this router is additive and namespaced under ``/api``.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, delete, func, or_, select

from app.controls import effective_settings, get_or_create_control, update_control
from app.db import engine
from app.models import Application, ApplicationStatus, Job, Profile, utcnow
from app.pipeline.apply import apply_one
from app.pipeline.orchestrator import start_discover_and_rank
from app.pipeline.state import PIPELINE_STATE, apply_progress, apply_start

router = APIRouter(prefix="/api")
log = logging.getLogger("api")

# Clean, text-only status labels for the SPA (no emoji — the UI styles badges).
STATUS_LABELS = {
    "ranked": "Ranked",
    "tailored": "Tailored",
    "queued": "Filled (dry-run)",
    "submitted": "Submitted",
    "needs_human": "Needs you",
    "failed": "Failed",
}
STAGE_LABELS = {
    "starting": "Starting",
    "tailoring": "Tailoring résumé",
    "submitting": "Submitting",
}
RESULT_LABELS = {
    "submitted": "Submitted",
    "dry_run": "Filled (dry-run)",
    "needs_human": "Needs you",
    "failed": "Failed",
    "skipped_cap": "Daily cap reached",
    "blocked": "Blocked (kill switch)",
    "retry_tailor": "Rate-limited — retry",
    "no_profile": "No profile",
    "missing": "Error",
}
# Status "tone" drives the badge colour in the UI.
STATUS_TONE = {
    "submitted": "success",
    "queued": "success",
    "dry_run": "success",
    "needs_human": "warning",
    "failed": "danger",
    "blocked": "danger",
    "retry_tailor": "warning",
    "skipped_cap": "warning",
    "ranked": "neutral",
    "tailored": "neutral",
}
_TERMINAL = {
    ApplicationStatus.submitted.value,
    ApplicationStatus.queued.value,
    ApplicationStatus.needs_human.value,
    ApplicationStatus.failed.value,
}

# Sensible defaults for answer-bank fields a CV usually can't provide. Shown
# pre-filled in Setup so the user can overwrite them; lowest precedence when
# merging with CV-derived and manually-entered answers.
DEFAULT_ANSWER_BANK = {
    "salary_expectation": "Negotiable",
    "how did you hear about us": "LinkedIn",
    "willing to relocate": "No",
    "notice period": "2 weeks",
    "earliest start date": "2 weeks",
    "authorized to work": "Yes",
    "require sponsorship": "No",
}


def _effective_bank(profile: Profile | None) -> dict:
    """Answer bank with defaults filled in for anything missing (editable)."""
    saved = profile.answer_bank if (profile and profile.answer_bank) else {}
    return {**DEFAULT_ANSWER_BANK, **saved}


def _count(session: Session, status: str | None = None) -> int:
    stmt = select(func.count()).select_from(Application)
    if status is not None:
        stmt = stmt.where(Application.status == status)
    return session.exec(stmt).one()


def status_state(app_id: int, status: str) -> dict:
    """JSON equivalent of matches.status_fragment: current label, stage and whether
    the client should keep polling."""
    prog = apply_progress(app_id)
    if prog:
        if not prog.get("done"):
            stage = STAGE_LABELS.get(prog.get("stage", ""), "Working")
            return {
                "app_id": app_id,
                "status": status,
                "label": stage,
                "elapsed": prog.get("elapsed", 0),
                "running": True,
                "polling": True,
                "tone": "neutral",
            }
        result = prog.get("result") or status
        return {
            "app_id": app_id,
            "status": status,
            "label": RESULT_LABELS.get(result, STATUS_LABELS.get(result, result)),
            "elapsed": prog.get("elapsed", 0),
            "running": False,
            "polling": False,
            "tone": STATUS_TONE.get(result, "neutral"),
        }
    return {
        "app_id": app_id,
        "status": status,
        "label": STATUS_LABELS.get(status, status),
        "elapsed": 0,
        "running": False,
        "polling": False,
        "tone": STATUS_TONE.get(status, "neutral"),
    }


@router.get("/dashboard")
def dashboard() -> dict:
    log.debug("GET /api/dashboard")
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
        settings = effective_settings(session)
        profile_configured = session.exec(select(Profile)).first() is not None
    return {
        "stats": stats,
        "settings": {
            "dry_run": settings.dry_run,
            "submit_kill_switch": settings.submit_kill_switch,
            "daily_submit_cap": settings.daily_submit_cap,
            "match_threshold": settings.match_threshold,
        },
        "profile_configured": profile_configured,
        "pipeline": PIPELINE_STATE.snapshot(),
    }


@router.get("/pipeline/status")
def pipeline_status() -> dict:
    log.debug("GET /api/pipeline/status")
    return PIPELINE_STATE.snapshot()


@router.post("/pipeline/run")
def pipeline_run() -> dict:
    from app.pipeline.orchestrator import start_pipeline_run

    started = start_pipeline_run()  # full cycle; no-op if already running
    log.info("Pipeline run requested (POST /api/pipeline/run) — started=%s", started)
    return {"ok": True, "started": started, "running": PIPELINE_STATE.snapshot()["running"]}


# --------------------------------------------------------------------------- #
# Settings: runtime controls (safety switches, cap, threshold, scheduler) that
# override the .env defaults live — see app.controls.
# --------------------------------------------------------------------------- #

class SettingsPayload(BaseModel):
    dry_run: bool = False
    submit_kill_switch: bool = False
    scheduler_enabled: bool = False
    daily_submit_cap: int = Field(40, ge=0, le=1000)
    match_threshold: int = Field(70, ge=0, le=100)
    cycle_interval_minutes: int = Field(60, ge=1, le=1440)


def _control_dict(control) -> dict:
    return {
        "dry_run": control.dry_run,
        "submit_kill_switch": control.submit_kill_switch,
        "scheduler_enabled": control.scheduler_enabled,
        "daily_submit_cap": control.daily_submit_cap,
        "match_threshold": control.match_threshold,
        "cycle_interval_minutes": control.cycle_interval_minutes,
    }


@router.get("/settings")
def settings_get() -> dict:
    log.debug("GET /api/settings")
    with Session(engine) as session:
        return _control_dict(get_or_create_control(session))


@router.post("/settings")
def settings_save(payload: SettingsPayload) -> dict:
    log.info(
        "Settings updated via API: dry_run=%s, kill_switch=%s, scheduler=%s, "
        "cap=%d, threshold=%d, interval=%dm",
        payload.dry_run, payload.submit_kill_switch, payload.scheduler_enabled,
        payload.daily_submit_cap, payload.match_threshold, payload.cycle_interval_minutes,
    )
    with Session(engine) as session:
        return _control_dict(update_control(session, **payload.model_dump()))


@router.get("/jobs")
def jobs_list() -> dict:
    log.debug("GET /api/jobs")
    with Session(engine) as session:
        rows = session.exec(
            select(Job, Application)
            .join(Application, isouter=True)
            .where(or_(Application.id.is_(None),
               Application.status != ApplicationStatus.submitted.value))
            .order_by(Application.match_score.desc(), Job.discovered_at.desc())
        ).all()
        running = PIPELINE_STATE.snapshot()["running"]
        jobs = []
        for job, app in rows:
            application = None
            if app is not None:
                application = {
                    "id": app.id,
                    "status": app.status,
                    "match_score": app.match_score,
                    "score_rationale": app.score_rationale,
                    "state": status_state(app.id, app.status),
                    "can_apply": app.status in ("ranked", "tailored"),
                }
            jobs.append(
                {
                    "id": job.id,
                    "title": job.title,
                    "company": job.company,
                    "location": job.location,
                    "source": job.source,
                    "remote": job.remote,
                    "apply_url": job.apply_url,
                    "application": application,
                }
            )
    return {"running": running, "jobs": jobs}


@router.post("/jobs/discover")
def jobs_discover() -> dict:
    log.info("Discover requested (POST /api/jobs/discover)")
    start_discover_and_rank()  # non-blocking; no-op if already running
    return {"ok": True, "running": PIPELINE_STATE.snapshot()["running"]}


@router.post("/jobs/clear")
def jobs_clear() -> dict:
    with Session(engine) as session:
        apps_deleted = session.exec(delete(Application)).rowcount
        jobs_deleted = session.exec(delete(Job)).rowcount
        session.commit()
    log.info("Cleared job pool: %d jobs, %d applications deleted", jobs_deleted, apps_deleted)
    return {"ok": True}


@router.post("/applications/rank")
def applications_rank() -> dict:
    from app.config import get_preferences
    from app.llm.ranking import rank_jobs

    log.info("Ranking requested (POST /api/applications/rank)")
    prefs = get_preferences()
    with Session(engine) as session:
        profile = session.exec(select(Profile)).first()
        if profile is None:
            log.warning("Ranking rejected: no profile configured")
            raise HTTPException(status_code=400, detail="No profile configured")
        rank_jobs(session, profile, prefs)
    log.info("Ranking complete")
    return {"ok": True}


@router.post("/matches/{app_id}/apply")
def matches_apply(app_id: int, background_tasks: BackgroundTasks) -> dict:
    with Session(engine) as session:
        app = session.get(Application, app_id)
        if app is None:
            log.warning("Apply requested for unknown application %s", app_id)
            raise HTTPException(status_code=404)
        status = app.status
    log.info("Apply queued for application %s (status=%s)", app_id, status)
    apply_start(app_id)
    background_tasks.add_task(apply_one, app_id)
    return status_state(app_id, status)


@router.get("/matches/{app_id}/status")
def matches_status(app_id: int) -> dict:
    log.debug("GET /api/matches/%s/status", app_id)
    with Session(engine) as session:
        app = session.get(Application, app_id)
        if app is None:
            raise HTTPException(status_code=404)
        return status_state(app_id, app.status)


@router.post("/matches/{app_id}/retry")
def matches_retry(app_id: int, background_tasks: BackgroundTasks) -> dict:
    with Session(engine) as session:
        app = session.get(Application, app_id)
        if app is None:
            log.warning("Retry requested for unknown application %s", app_id)
            raise HTTPException(status_code=404)
        if app.status in (ApplicationStatus.failed.value, ApplicationStatus.needs_human.value):
            app.status = (
                ApplicationStatus.tailored.value if app.resume_path
                else ApplicationStatus.ranked.value
            )
            app.needs_human_reason = ""
            session.add(app)
            session.commit()
        status_value = app.status
    log.info("Retry queued for application %s (status=%s)", app_id, status_value)
    apply_start(app_id)
    background_tasks.add_task(apply_one, app_id)
    return status_state(app_id, status_value)


# --------------------------------------------------------------------------- #
# Setup: profile, preferences and the answer bank used to fill application forms
# --------------------------------------------------------------------------- #

class SetupPreferences(BaseModel):
    desired_roles: list[str] = []
    locations: list[str] = []
    remote_preference: str = "any"
    min_salary: int | None = None
    salary_currency: str = "USD"
    require_sponsorship: bool = False
    work_authorization: str = ""
    greenhouse_companies: list[str] = []
    lever_companies: list[str] = []


class SetupPayload(BaseModel):
    preferences: SetupPreferences
    # Free-form key -> value bank; keys are matched (by the LLM) to arbitrary
    # application form fields, so descriptive keys improve coverage.
    answer_bank: dict[str, str] = {}


def _profile_summary(profile: Profile | None) -> dict | None:
    if profile is None:
        return None
    return {
        "full_name": profile.full_name,
        "email": profile.email,
        "phone": profile.phone,
        "skills": len(profile.skills or []),
        "experience": len(profile.experience or []),
        "education": len(profile.education or []),
        "resume_filename": os.path.basename(profile.base_resume_path)
        if profile.base_resume_path
        else "",
    }


@router.get("/setup")
def setup_get() -> dict:
    log.debug("GET /api/setup")
    from app.config import RemotePreference, SourceConfig, get_preferences

    with Session(engine) as session:
        profile = session.exec(select(Profile)).first()
    prefs = get_preferences()
    gh = prefs.sources.get("greenhouse", SourceConfig())
    lever = prefs.sources.get("lever", SourceConfig())
    return {
        "profile": _profile_summary(profile),
        "preferences": {
            "desired_roles": prefs.desired_roles,
            "locations": prefs.locations,
            "remote_preference": prefs.remote_preference.value,
            "min_salary": prefs.min_salary,
            "salary_currency": prefs.salary_currency,
            "require_sponsorship": prefs.require_sponsorship,
            "work_authorization": prefs.work_authorization,
            "greenhouse_companies": gh.companies,
            "lever_companies": lever.companies,
        },
        "answer_bank": _effective_bank(profile),
        "remote_options": [r.value for r in RemotePreference],
    }


@router.post("/setup")
def setup_save(payload: SetupPayload) -> dict:
    from app.config import RemotePreference, SourceConfig, get_preferences

    p = payload.preferences
    prefs = get_preferences()
    prefs.desired_roles = [s.strip() for s in p.desired_roles if s.strip()]
    prefs.locations = [s.strip() for s in p.locations if s.strip()]
    try:
        prefs.remote_preference = RemotePreference(p.remote_preference)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid remote preference: {p.remote_preference}")
    prefs.min_salary = p.min_salary
    prefs.salary_currency = p.salary_currency or "USD"
    prefs.require_sponsorship = p.require_sponsorship
    prefs.work_authorization = p.work_authorization

    # Update only the company lists for the boards this form controls, preserving
    # each source's enabled/autonomy flags and any other configured sources.
    gh = prefs.sources.get("greenhouse") or SourceConfig(enabled=True, autonomy=True)
    gh.companies = [s.strip() for s in p.greenhouse_companies if s.strip()]
    prefs.sources["greenhouse"] = gh
    lever = prefs.sources.get("lever") or SourceConfig(enabled=True, autonomy=True)
    lever.companies = [s.strip() for s in p.lever_companies if s.strip()]
    prefs.sources["lever"] = lever
    prefs.save()
    log.info(
        "Setup saved via API: %d roles, %d locations, %d greenhouse + %d lever companies",
        len(prefs.desired_roles), len(prefs.locations),
        len(gh.companies), len(lever.companies),
    )

    clean_bank = {
        k.strip(): v.strip()
        for k, v in payload.answer_bank.items()
        if k.strip() and v.strip()
    }
    with Session(engine) as session:
        profile = session.exec(select(Profile)).first() or Profile()
        profile.preferences = prefs.model_dump(mode="json")
        profile.answer_bank = {**DEFAULT_ANSWER_BANK, **clean_bank}
        profile.updated_at = utcnow()
        session.add(profile)
        session.commit()
    log.info("Profile answer bank saved: %d fields", len(clean_bank))
    return {"ok": True}


@router.post("/setup/resume")
async def setup_resume(resume: UploadFile = File(...)) -> dict:
    """Upload + parse a résumé, upserting the Profile. Returns the refreshed
    profile summary and answer bank so the form can repopulate."""
    print("printing resume.filename", resume.filename)
    from app.config import get_settings
    from app.resume.parse import parse_resume

    if not resume.filename:
        log.warning("Resume upload rejected: no file provided")
        raise HTTPException(status_code=400, detail="No file provided")
    settings = get_settings()
    settings.ensure_dirs()
    dest = settings.resumes_dir / resume.filename
    contents = await resume.read()
    dest.write_bytes(contents)
    log.info("Resume uploaded via API: %s (%d KB) — parsing…", resume.filename, len(contents) // 1024)
    fields = parse_resume(dest) or {}
    cv_bank = fields.pop("answer_bank", {})
    log.info(
        "Resume parsed: name=%r, %d skills, %d experience entries",
        fields.get("full_name") or "(none)",
        len(fields.get("skills") or []),
        len(fields.get("experience") or []),
    )

    with Session(engine) as session:
        profile = session.exec(select(Profile)).first() or Profile()
        existing = dict(profile.answer_bank or {})
        for key, value in fields.items():
            setattr(profile, key, value)
        # Precedence: prior manual entries > CV-derived > defaults.
        profile.answer_bank = {**DEFAULT_ANSWER_BANK, **cv_bank, **existing}
        profile.updated_at = utcnow()
        session.add(profile)
        session.commit()
        session.refresh(profile)
        return {"profile": _profile_summary(profile), "answer_bank": dict(profile.answer_bank)}


# --------------------------------------------------------------------------- #
# Applications: every application with its status, error, and generated artifacts
# --------------------------------------------------------------------------- #

def _error_of(app: Application) -> str:
    """Human-readable failure reason for a failed / needs-human application."""
    if app.status == ApplicationStatus.needs_human.value:
        return app.needs_human_reason or ""
    if app.status == ApplicationStatus.failed.value:
        for ev in reversed(app.events or []):
            if ev.get("event") == "failed":
                return ev.get("error", "")
    return ""


@router.get("/applications")
def applications_list() -> dict:
    log.debug("GET /api/applications")
    with Session(engine) as session:
        rows = session.exec(
            select(Application, Job).join(Job).order_by(Application.match_score.desc())
        ).all()
        apps = [
            {
                "id": app.id,
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "source": job.source,
                "apply_url": job.apply_url,
                "status": app.status,
                "match_score": app.match_score,
                "score_rationale": app.score_rationale,
                "error": _error_of(app),
                "has_resume": bool(app.resume_path),
                "has_cover_letter": bool(app.cover_letter_path),
                "submitted_at": app.submitted_at.isoformat() if app.submitted_at else None,
                "state": status_state(app.id, app.status),
                "can_retry": app.status in (
                    ApplicationStatus.failed.value, ApplicationStatus.needs_human.value
                ),
            }
            for app, job in rows
        ]
    return {"applications": apps}


@router.post("/applications/tailor")
def applications_tailor() -> dict:
    from app.pipeline.tailor import tailor_ranked

    log.info("Tailor-ranked requested (POST /api/applications/tailor)")
    with Session(engine) as session:
        settings = effective_settings(session)
        profile = session.exec(select(Profile)).first()
        if profile is None:
            log.warning("Tailor skipped: no profile configured")
            raise HTTPException(status_code=400, detail="No profile configured")
        result = tailor_ranked(session, profile, settings)
    log.info("Tailor pass complete: %s", result)
    return {"ok": True, **result}


@router.post("/applications/submit")
def applications_submit() -> dict:
    from app.config import get_preferences
    from app.pipeline.submit import submit_tailored

    log.info("Submit-tailored requested (POST /api/applications/submit)")
    prefs = get_preferences()
    with Session(engine) as session:
        settings = effective_settings(session)
        profile = session.exec(select(Profile)).first()
        if profile is None:
            log.warning("Submit skipped: no profile configured")
            raise HTTPException(status_code=400, detail="No profile configured")
        result = submit_tailored(session, profile, prefs, settings)
    log.info("Submit pass complete (dry_run=%s): %s", settings.dry_run, result)
    return {"ok": True, "dry_run": settings.dry_run, **result}


@router.get("/applications/{app_id}/{artifact}")
def download_artifact(app_id: int, artifact: str) -> FileResponse:
    log.debug("Artifact download: app %s, %s", app_id, artifact)
    if artifact not in ("resume", "cover_letter"):
        log.warning("Artifact download rejected: unknown artifact %r (app %s)", artifact, app_id)
        raise HTTPException(status_code=404)
    with Session(engine) as session:
        app = session.get(Application, app_id)
        if app is None:
            log.warning("Artifact download for unknown application %s", app_id)
            raise HTTPException(status_code=404)
        path = app.resume_path if artifact == "resume" else app.cover_letter_path
    if not path:
        raise HTTPException(status_code=404, detail="Artifact not generated yet")
    return FileResponse(path, media_type="application/pdf", filename=f"{artifact}_{app_id}.pdf")


# --------------------------------------------------------------------------- #
# Intervention: the queue of applications that need the user (captcha/essay/etc.)
# --------------------------------------------------------------------------- #

@router.get("/intervention")
def intervention_list() -> dict:
    log.debug("GET /api/intervention")
    with Session(engine) as session:
        rows = session.exec(
            select(Application, Job).join(Job).where(
                Application.status == ApplicationStatus.needs_human.value
            ).order_by(Application.updated_at.desc())
        ).all()
        items = [
            {
                "id": app.id,
                "title": job.title,
                "company": job.company,
                "apply_url": job.apply_url,
                "source": job.source,
                "ats_type": job.ats_type,
                "reason": app.needs_human_reason or "",
                "has_resume": bool(app.resume_path),
            }
            for app, job in rows
        ]
    return {"items": items}


def _answers_for(app: Application, job: Job, profile: Profile | None) -> list[dict]:
    """Planned answers for the assist browser: prefer the preview stored during
    the failed auto-submit, else re-plan (DRY_RUN) for the schema-driven Ashby ATS."""
    for ev in reversed(app.events or []):
        if ev.get("event") == "application_preview":
            return (ev.get("preview") or {}).get("answers", []) or []
    if profile is not None and job.ats_type == "ashby":
        try:
            from app.config import Settings
            from app.submit import ashby_api

            _, preview = ashby_api.build_and_submit(
                job, profile, Settings(dry_run=True), resume_path=app.resume_path
            )
            return preview.get("answers", []) or []
        except Exception as exc:  # noqa: BLE001
            log.warning("Assist re-plan failed (app %s): %s", app.id, exc)
    return []


@router.post("/intervention/{app_id}/assist")
def intervention_assist(app_id: int) -> dict:
    """Enqueue the application in the managed assist browser (co-browse handoff).

    The live page is streamed over ``/ws/assist/{app_id}``; here we only plan the
    answers and queue the job. Returns the current session snapshot.
    """
    from app.submit import assist_session

    with Session(engine) as session:
        app = session.get(Application, app_id)
        if app is None:
            log.warning("Assist requested for unknown application %s", app_id)
            raise HTTPException(status_code=404)
        job = session.get(Job, app.job_id)
        profile = session.exec(select(Profile)).first()
        apply_url = job.apply_url if job else ""
        answers = _answers_for(app, job, profile) if job else []
        resume_path = app.resume_path
    if not apply_url:
        raise HTTPException(status_code=400, detail="No apply URL for this job")
    # Only enqueue if not already active/queued for this app (avoid double-add).
    stage = assist_session.snapshot(app_id).get("stage")
    if stage not in ("queued", "opening", "live"):
        assist_session.enqueue(app_id, apply_url, answers, resume_path)
        log.info("Assist session enqueued for application %s (%d planned answers)", app_id, len(answers))
    else:
        log.info("Assist session for application %s already %s", app_id, stage)
    return assist_session.snapshot(app_id)


@router.get("/intervention/{app_id}/assist-status")
def intervention_assist_status(app_id: int) -> dict:
    from app.submit import assist_session

    return assist_session.snapshot(app_id)


@router.post("/intervention/{app_id}/done")
def intervention_done(app_id: int) -> dict:
    """Mark a needs-human application as manually submitted."""
    with Session(engine) as session:
        app = session.get(Application, app_id)
        if app is None:
            log.warning("Mark-done for unknown application %s", app_id)
            raise HTTPException(status_code=404)
        app.status = ApplicationStatus.submitted.value
        app.submitted_at = utcnow()
        app.events = [*app.events, {"event": "manually_submitted", "at": app.submitted_at.isoformat()}]
        session.add(app)
        session.commit()
    log.info("Application %s marked manually submitted", app_id)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Summary: the daily digest of activity (also emailable if SMTP is configured)
# --------------------------------------------------------------------------- #

@router.get("/summary")
def summary_get() -> dict:
    from app.config import get_settings
    from app.tracking.reporting import build_summary

    log.debug("GET /api/summary")
    settings = get_settings()
    with Session(engine) as session:
        s = build_summary(session)
    return {
        "day": s.day.isoformat(),
        "discovered": s.discovered,
        "ranked": s.ranked,
        "tailored": s.tailored,
        "submitted": s.submitted,
        "failed_today": s.failed_today,
        "needs_human_today": s.needs_human_today,
        "needs_human_open": s.needs_human_open,
        "top_matches": [
            {
                "score": app.match_score,
                "company": job.company,
                "title": job.title,
                "status": app.status,
            }
            for app, job in s.top_matches
        ],
        "email_configured": bool(
            settings.smtp_host and settings.summary_email_to and settings.smtp_from
        ),
    }


@router.post("/summary/email")
def summary_email() -> dict:
    from app.config import get_settings
    from app.tracking.reporting import build_summary, send_summary_email

    log.info("Summary email requested (POST /api/summary/email)")
    with Session(engine) as session:
        s = build_summary(session)
    sent = send_summary_email(get_settings(), s)
    if not sent:
        log.warning("Summary email not sent: SMTP not configured")
        raise HTTPException(status_code=400, detail="SMTP is not configured (set SMTP_* in .env)")
    log.info("Summary email dispatched")
    return {"ok": True, "sent": True}
