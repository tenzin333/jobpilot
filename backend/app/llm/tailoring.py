"""Tailor a resume + cover letter for a specific job.

Anti-fabrication is enforced two ways:
1. The prompt + schema constrain output to the candidate's real profile.
2. A post-check strips any highlighted skill not present in the profile
   (skills list or raw resume text), returning what was removed for the audit log.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.llm.client import parse_structured
from app.llm.ranking import build_profile_prefix
from app.models import Job, Profile


class RoleBullets(BaseModel):
    company: str = ""
    title: str = ""
    bullets: list[str] = Field(default_factory=list)


class TailoredResume(BaseModel):
    summary: str = ""
    highlighted_skills: list[str] = Field(default_factory=list)
    roles: list[RoleBullets] = Field(default_factory=list)
    cover_letter: str = ""


_TAILOR_SYSTEM_SUFFIX = (
    "\n\nTailor the candidate's resume and write a cover letter for the job below. "
    "Rules: use ONLY the candidate's real experience, skills, and accomplishments. "
    "You may reorder, re-emphasize, and rephrase existing content to match the job, "
    "but NEVER invent employers, titles, dates, skills, metrics, or achievements. "
    "Highlighted skills must be ones the candidate actually has."
)


def _profile_skill_corpus(profile: Profile) -> str:
    return (" ".join(profile.skills) + " " + (profile.raw_text or "")).lower()


def verify_no_fabrication(tailored: TailoredResume, profile: Profile) -> list[str]:
    """Return highlighted skills not found in the profile (skills list or raw text)."""
    corpus = _profile_skill_corpus(profile)
    return [s for s in tailored.highlighted_skills if s.lower() not in corpus]


def tailor(job: Job, profile: Profile) -> tuple[TailoredResume, list[str]]:
    """Generate tailored content. Returns (tailored, removed_fabricated_skills)."""
    system = build_profile_prefix(profile) + _TAILOR_SYSTEM_SUFFIX
    user = (
        f"Job:\nCompany: {job.company}\nTitle: {job.title}\nLocation: {job.location}\n"
        f"Description:\n{job.description[:4000]}"
    )
    tailored = parse_structured(
        system=system, user=user, schema=TailoredResume, cache_system=True, max_tokens=2500
    )

    removed = verify_no_fabrication(tailored, profile)
    if removed:
        keep = {s.lower() for s in tailored.highlighted_skills} - {s.lower() for s in removed}
        tailored.highlighted_skills = [s for s in tailored.highlighted_skills if s.lower() in keep]
    return tailored, removed
