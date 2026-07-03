"""JSON API for the React (Vite) frontend.

These endpoints mirror the data the existing HTMX pages render, but return JSON so
the SPA in ``frontend/`` can consume them. The legacy server-rendered pages remain
untouched; this router is additive and namespaced under ``/api``.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, delete, func, select

from app.controls import effective_settings
from app.db import engine
from app.models import Application, ApplicationStatus, Job, Profile, utcnow
from app.pipeline.apply import apply_one
from app.pipeline.orchestrator import start_discover_and_rank
from app.pipeline.state import PIPELINE_STATE, apply_progress, apply_start

router = APIRouter(prefix="/api")

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
    return PIPELINE_STATE.snapshot()


@router.get("/jobs")
def jobs_list() -> dict:
    with Session(engine) as session:
        rows = session.exec(
            select(Job, Application)
            .join(Application, isouter=True)
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
    start_discover_and_rank()  # non-blocking; no-op if already running
    return {"ok": True, "running": PIPELINE_STATE.snapshot()["running"]}


@router.post("/jobs/clear")
def jobs_clear() -> dict:
    with Session(engine) as session:
        session.exec(delete(Application))
        session.exec(delete(Job))
        session.commit()
    return {"ok": True}


@router.post("/applications/rank")
def applications_rank() -> dict:
    from app.config import get_preferences
    from app.llm.ranking import rank_jobs

    prefs = get_preferences()
    with Session(engine) as session:
        profile = session.exec(select(Profile)).first()
        if profile is None:
            raise HTTPException(status_code=400, detail="No profile configured")
        rank_jobs(session, profile, prefs)
    return {"ok": True}


@router.post("/matches/{app_id}/apply")
def matches_apply(app_id: int, background_tasks: BackgroundTasks) -> dict:
    with Session(engine) as session:
        app = session.get(Application, app_id)
        if app is None:
            raise HTTPException(status_code=404)
        status = app.status
    apply_start(app_id)
    background_tasks.add_task(apply_one, app_id)
    return status_state(app_id, status)


@router.get("/matches/{app_id}/status")
def matches_status(app_id: int) -> dict:
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
    from app.config import RemotePreference, SourceConfig, get_preferences
    from app.web.setup import _effective_bank

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
    from app.web.setup import DEFAULT_ANSWER_BANK

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
    return {"ok": True}


@router.post("/setup/resume")
async def setup_resume(resume: UploadFile = File(...)) -> dict:
    """Upload + parse a résumé, upserting the Profile. Returns the refreshed
    profile summary and answer bank so the form can repopulate."""
    from app.config import get_settings
    from app.resume.parse import parse_resume
    from app.web.setup import DEFAULT_ANSWER_BANK

    if not resume.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    settings = get_settings()
    settings.ensure_dirs()
    dest = settings.resumes_dir / resume.filename
    dest.write_bytes(await resume.read())
    fields = parse_resume(dest) or {}
    cv_bank = fields.pop("answer_bank", {})

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
