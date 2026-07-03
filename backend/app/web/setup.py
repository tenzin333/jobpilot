"""Setup page: upload base resume + edit job-search preferences."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.config import Preferences, RemotePreference, SourceConfig, get_preferences, get_settings
from app.db import engine
from app.models import Profile
from app.resume.parse import parse_resume

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
log = logging.getLogger("setup")

# Answer-bank keys the form renders as dedicated inputs.
TYPED_KEYS = {
    "linkedin", "github", "portfolio", "current_location",
    "years_experience", "salary_expectation", "current_employer", "current_title",
}

# Sensible defaults for fields a CV usually can't provide. Shown pre-filled in
# Setup so the user can overwrite them; lowest precedence when merging.
DEFAULT_ANSWER_BANK = {
    "salary_expectation": "Negotiable",
    "how did you hear about us": "LinkedIn",
    "willing to relocate": "No",
    "notice period": "2 weeks",
    "earliest start date": "2 weeks",
    "authorized to work": "Yes",
    "require sponsorship": "No",
}


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _effective_bank(profile: Profile | None) -> dict:
    """Answer bank with defaults filled in for anything missing (editable)."""
    saved = profile.answer_bank if (profile and profile.answer_bank) else {}
    return {**DEFAULT_ANSWER_BANK, **saved}


@router.get("/setup", response_class=HTMLResponse)
def setup_get(request: Request) -> HTMLResponse:
    with Session(engine) as session:
        profile = session.exec(select(Profile)).first()
    prefs = get_preferences()
    bank = _effective_bank(profile)
    # Free-form (non-typed) answers, shown one-per-line for editing.
    extra_answers = "\n".join(f"{k}: {v}" for k, v in bank.items() if k not in TYPED_KEYS)
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "profile": profile,
            "prefs": prefs,
            "bank": bank,
            "extra_answers": extra_answers,
            "remote_options": [r.value for r in RemotePreference],
            "gh": prefs.sources.get("greenhouse", SourceConfig()),
            "lever": prefs.sources.get("lever", SourceConfig()),
        },
    )


@router.post("/setup")
async def setup_post(
    request: Request,
    desired_roles: str = Form(""),
    locations: str = Form(""),
    remote_preference: str = Form("any"),
    min_salary: str = Form(""),
    salary_currency: str = Form("USD"),
    require_sponsorship: bool = Form(False),
    work_authorization: str = Form(""),
    greenhouse_companies: str = Form(""),
    lever_companies: str = Form(""),
    # Answer bank (used to fill arbitrary application form fields).
    linkedin_url: str = Form(""),
    github_url: str = Form(""),
    portfolio_url: str = Form(""),
    current_location: str = Form(""),
    years_experience: str = Form(""),
    salary_expectation: str = Form(""),
    current_employer: str = Form(""),
    current_title: str = Form(""),
    additional_answers: str = Form(""),
    resume: UploadFile | None = None,
) -> RedirectResponse:
    
    log.info("starting setup")
    settings = get_settings()
    log.info("Setup submitted (resume=%s)", resume.filename if resume and resume.filename else "none")

    # 1. MERGE into existing preferences (don't clobber aggregator sources like
    #    themuse/remotive/adzuna/career_pages that this form doesn't edit).
    prefs = get_preferences()
    prefs.desired_roles = _lines(desired_roles)
    prefs.locations = _lines(locations)
    prefs.remote_preference = RemotePreference(remote_preference)
    prefs.min_salary = int(min_salary) if min_salary.strip().isdigit() else None
    prefs.salary_currency = salary_currency or "USD"
    prefs.require_sponsorship = require_sponsorship
    prefs.work_authorization = work_authorization

    # Update only the company lists for the board sources the form controls,
    # preserving each source's enabled/autonomy and all other sources.
    gh = prefs.sources.get("greenhouse") or SourceConfig(enabled=True, autonomy=True)
    gh.companies = _lines(greenhouse_companies)
    prefs.sources["greenhouse"] = gh

    lever = prefs.sources.get("lever") or SourceConfig(enabled=True, autonomy=True)
    lever.companies = _lines(lever_companies)
    prefs.sources["lever"] = lever

    prefs.save()
    log.info("Preferences saved: %d roles, %d locations, sources=%s",
             len(prefs.desired_roles), len(prefs.locations), list(prefs.sources.keys()))

    # 2. If a resume was uploaded, parse it and upsert the Profile.
    resume_fields: dict | None = None
    if resume is not None and resume.filename:
        contents = await resume.read()
        dest = settings.resumes_dir / resume.filename
        dest.write_bytes(contents)
        log.info("Resume uploaded: %s (%d KB) - extracting...", resume.filename, len(contents) // 1024)
        resume_fields = parse_resume(dest)
        log.info(
            "Resume parsed: name=%r, %d skills, %d roles, %d answer-bank fields",
            resume_fields.get("full_name") or "(none)",
            len(resume_fields.get("skills") or []),
            len(resume_fields.get("experience") or []),
            len(resume_fields.get("answer_bank") or {}),
        )

    # 3. Build the answer bank from the form (used to fill application fields).
    answer_bank: dict[str, str] = {
        k: v
        for k, v in {
            "linkedin": linkedin_url,
            "github": github_url,
            "portfolio": portfolio_url,
            "current_location": current_location,
            "years_experience": years_experience,
            "salary_expectation": salary_expectation,
            "current_employer": current_employer,
            "current_title": current_title,
        }.items()
        if v.strip()
    }
    for line in additional_answers.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            if key.strip() and val.strip():
                answer_bank[key.strip()] = val.strip()

    # CV-derived answers are separated so we can control precedence below.
    cv_answer_bank: dict = resume_fields.pop("answer_bank", {}) if resume_fields else {}

    with Session(engine) as session:
        profile = session.exec(select(Profile)).first() or Profile()
        existing_bank = dict(profile.answer_bank or {})
        if resume_fields:
            for key, value in resume_fields.items():
                setattr(profile, key, value)
        profile.preferences = prefs.model_dump(mode="json")
        # Precedence: form input > prior manual > CV-derived > defaults.
        profile.answer_bank = {**DEFAULT_ANSWER_BANK, **cv_answer_bank, **existing_bank, **answer_bank}
        profile.updated_at = datetime.now(timezone.utc)
        session.add(profile)
        session.commit()
        log.info("Profile saved (id=%s, %d answer-bank fields)", profile.id, len(profile.answer_bank or {}))

    return RedirectResponse(url="/setup", status_code=303)
