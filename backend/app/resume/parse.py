"""Parse a base resume (PDF/DOCX) into structured Profile fields.

Two steps:
1. Extract raw text (pdfplumber for PDF, python-docx for DOCX).
2. Use an Opus structured-output pass to produce clean structured fields.

If Claude is unavailable (no API key), we still store raw text + empty structured
fields so the app remains usable for the rest of setup.
"""
from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from app.llm.client import parse_structured

log = logging.getLogger(__name__)


class ExperienceItem(BaseModel):
    company: str = ""
    title: str = ""
    start: str = ""
    end: str = ""
    bullets: list[str] = Field(default_factory=list)


class EducationItem(BaseModel):
    institution: str = ""
    degree: str = ""
    year: str = ""


class ResumeExtraction(BaseModel):
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    years_experience: str = ""
    skills: list[str] = Field(default_factory=list)
    experience: list[ExperienceItem] = Field(default_factory=list)
    education: list[EducationItem] = Field(default_factory=list)


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages).strip()
    if suffix in (".docx", ".doc"):
        import docx

        document = docx.Document(str(path))
        return "\n".join(p.text for p in document.paragraphs).strip()
    raise ValueError(f"Unsupported resume format: {suffix} (use .pdf or .docx)")


_EXTRACT_SYSTEM = (
    "You extract structured data from a resume. Return ONLY information present "
    "in the resume text. Do not invent skills, employers, dates, degrees, links, "
    "or locations. Preserve the candidate's wording for experience bullets. "
    "For links, return the LinkedIn/GitHub/portfolio URLs if present. For "
    "years_experience, give the candidate's total professional experience as a "
    "number if it can be inferred from the work history, else leave blank."
)


def _answer_bank_from_extraction(ext: ResumeExtraction) -> dict[str, str]:
    """Derive common application answers from the parsed resume."""
    bank: dict[str, str] = {}
    if ext.linkedin:
        bank["linkedin"] = ext.linkedin
    if ext.github:
        bank["github"] = ext.github
    if ext.portfolio:
        bank["portfolio"] = ext.portfolio
    if ext.location:
        bank["current_location"] = ext.location
    if ext.years_experience:
        bank["years_experience"] = ext.years_experience
    if ext.experience:
        latest = ext.experience[0]
        if latest.company:
            bank["current_employer"] = latest.company
        if latest.title:
            bank["current_title"] = latest.title
    return bank


def structured_extract(raw_text: str) -> ResumeExtraction:
    return parse_structured(
        system=_EXTRACT_SYSTEM,
        user=f"Resume text:\n\n{raw_text[:8000]}",
        schema=ResumeExtraction,
        tier="fast",        # 8B: extraction is simple + the 70B free tier is rate-limited
        max_tokens=2000,    # bounded output to fit the free-tier per-request cap
        cache_system=False,
    )


def parse_resume(path: Path) -> dict:
    """Return a dict of Profile fields parsed from the resume file.

    Falls back to raw-text-only when Claude is unavailable.
    """
    raw_text = extract_text(path)
    log.debug("Extracted %d chars of resume text from %s", len(raw_text), path.name)
    fields: dict = {
        "raw_text": raw_text,
        "base_resume_path": str(path),
        "full_name": "",
        "email": "",
        "phone": "",
        "skills": [],
        "experience": [],
        "education": [],
        "answer_bank": {},
    }
    try:
        extraction = structured_extract(raw_text)
        log.debug("Structured extraction succeeded for %s", path.name)
    except Exception as exc:  # noqa: BLE001
        # Never fail a resume upload because extraction errored (no key, network,
        # rate limit, bad model output). Raw text is still stored for the pipeline.
        log.warning("Resume extraction failed: %s", exc)
        return fields

    fields.update(
        full_name=extraction.full_name,
        email=extraction.email,
        phone=extraction.phone,
        skills=extraction.skills,
        experience=[e.model_dump() for e in extraction.experience],
        education=[e.model_dump() for e in extraction.education],
        answer_bank=_answer_bank_from_extraction(extraction),
    )
    return fields
