from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app.candidate_brain.models import Basic, Candidate, Education, Employment, Skills
from app.models import CandidateProfileReview, Profile, split_full_name, utcnow


class ProfileReview:
    """Stage, edit, approve, and discard one durable candidate interpretation."""

    def __init__(self, session: Session):
        self.session = session

    def get(self) -> CandidateProfileReview | None:
        return self.session.get(CandidateProfileReview, 1)

    def stage(
        self,
        candidate: Candidate,
        *,
        resume_path: str,
        raw_text: str,
        answer_bank: dict[str, Any] | None = None,
    ) -> CandidateProfileReview:
        validated = Candidate.model_validate(candidate.model_dump())
        row = self.get() or CandidateProfileReview()
        now = utcnow()
        row.status = "pending"
        row.pending_profile = validated.model_dump(mode="json")
        row.pending_resume_path = resume_path
        row.pending_raw_text = raw_text
        row.pending_answer_bank = dict(answer_bank or {})
        row.source_filename = Path(resume_path).name
        row.generated_at = now
        row.updated_at = now
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def update(self, candidate: Candidate) -> CandidateProfileReview:
        row = self.get()
        if row is None:
            raise ValueError("No candidate profile to update")
        row.pending_profile = Candidate.model_validate(candidate.model_dump()).model_dump(mode="json")
        row.status = "pending"
        row.updated_at = utcnow()
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def approve(self) -> Candidate:
        row = self.get()
        if row is None or not row.pending_profile:
            raise ValueError("No pending candidate profile to approve")
        candidate = Candidate.model_validate(row.pending_profile)
        profile = self.session.exec(select(Profile)).first() or Profile()
        self._apply(candidate, row, profile)
        row.approved_profile = candidate.model_dump(mode="json")
        row.approved_resume_path = row.pending_resume_path
        row.approved_raw_text = row.pending_raw_text
        row.approved_answer_bank = dict(row.pending_answer_bank)
        row.approved_source_filename = row.source_filename
        row.pending_profile = {}
        row.status = "approved"
        row.approved_at = utcnow()
        row.updated_at = row.approved_at
        self.session.add(profile)
        self.session.add(row)
        self.session.commit()
        return candidate

    def reject(self) -> None:
        row = self.get()
        if row is None or not row.pending_profile:
            raise ValueError("No pending candidate profile to discard")
        row.pending_profile = {}
        row.pending_resume_path = row.approved_resume_path
        row.pending_raw_text = row.approved_raw_text
        row.pending_answer_bank = dict(row.approved_answer_bank)
        row.source_filename = row.approved_source_filename
        row.status = "approved" if row.approved_profile else "missing"
        row.updated_at = utcnow()
        self.session.add(row)
        self.session.commit()

    @staticmethod
    def _apply(candidate: Candidate, row: CandidateProfileReview, profile: Profile) -> None:
        honorific, first, middle, last = split_full_name(candidate.profile.full_name)
        profile.full_name = candidate.profile.full_name
        profile.honorific = honorific
        profile.first_name = first
        profile.middle_name = middle
        profile.last_name = last
        profile.email = candidate.profile.email or ""
        profile.phone = candidate.profile.phone or ""
        profile.base_resume_path = row.pending_resume_path
        profile.raw_text = row.pending_raw_text
        profile.skills = _flatten_skills(candidate.skills)
        profile.experience = [{
            "company": role.company,
            "title": role.designation,
            "start": role.start_date_raw or "",
            "end": role.end_date_raw or "",
            "bullets": [
                *role.responsibilities,
                *(" ".join(filter(None, [item.description, item.metric])) for item in role.achievements),
            ],
        } for role in candidate.employment]
        profile.education = [item.model_dump(mode="json") for item in candidate.education]
        profile.answer_bank = {**row.pending_answer_bank, **(profile.answer_bank or {})}
        if candidate.profile.location and not profile.answer_bank.get("current_location"):
            profile.answer_bank["current_location"] = candidate.profile.location
        profile.updated_at = utcnow()


def _flatten_skills(skills: Skills) -> list[str]:
    values = [
        *skills.programming_languages,
        *skills.frameworks,
        *skills.databases,
        *skills.tools,
        *skills.ai_ml,
    ]
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def candidate_from_resume_fields(fields: dict[str, Any]) -> Candidate:
    """Adapt the existing LLM resume extraction into the review domain model."""
    employment = [Employment(
        company=str(item.get("company") or "Unspecified employer"),
        designation=str(item.get("title") or "Unspecified role"),
        start_date_raw=str(item.get("start") or "") or None,
        end_date_raw=str(item.get("end") or "") or None,
        responsibilities=[str(value) for value in item.get("bullets", []) if str(value).strip()],
    ) for item in fields.get("experience", [])]
    education = [Education(
        institution=str(item.get("institution") or "Unspecified institution"),
        degree=str(item.get("degree") or ""),
        year=str(item.get("year") or ""),
    ) for item in fields.get("education", [])]
    latest_title = employment[0].designation if employment else None
    return Candidate(
        profile=Basic(
            full_name=str(fields.get("full_name") or ""),
            email=str(fields.get("email") or "") or None,
            phone=str(fields.get("phone") or "") or None,
            location=str((fields.get("answer_bank") or {}).get("current_location") or "") or None,
            professional_title=latest_title,
        ),
        employment=employment,
        education=education,
        skills=Skills(tools=[str(value) for value in fields.get("skills", []) if str(value).strip()]),
    )


def candidate_from_profile(profile: Profile) -> Candidate:
    """Create a review draft for profiles that predate the review workflow."""
    fields = {
        "full_name": profile.full_name or " ".join(
            value for value in [profile.first_name, profile.middle_name, profile.last_name] if value
        ),
        "email": profile.email,
        "phone": profile.phone,
        "skills": profile.skills,
        "experience": profile.experience,
        "education": profile.education,
        "answer_bank": profile.answer_bank,
    }
    return candidate_from_resume_fields(fields)
