"""Schema-driven application answering.

Fetches a Greenhouse job's question schema, then uses the LLM (plus the
candidate's profile + answer bank) to produce an answer for every field —
including required dropdowns (country, work authorization, sponsorship). Code
validates coverage so unanswerable required questions route to a human instead
of submitting an incomplete application.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
from pydantic import BaseModel, Field

from app.llm.client import parse_structured
from app.models import Profile

GH_BASE = "https://boards-api.greenhouse.io/v1/boards"

# Field names Greenhouse maps to dedicated applicant fields (not custom questions).
CORE_FIELDS = {"first_name", "last_name", "email", "phone", "resume", "cover_letter"}


# --- schema models ------------------------------------------------------

class Option(BaseModel):
    label: str
    value: str = ""


class FormField(BaseModel):
    name: str
    type: str
    options: list[Option] = Field(default_factory=list)


class Question(BaseModel):
    label: str
    required: bool = False
    fields: list[FormField] = Field(default_factory=list)


# --- LLM output ---------------------------------------------------------

class Answer(BaseModel):
    name: str                       # field name from the schema
    value: str = ""                 # text answer or chosen option label
    values: list[str] = Field(default_factory=list)  # for multi-select


class AnswerSet(BaseModel):
    answers: list[Answer] = Field(default_factory=list)


def board_slug_from_url(url: str) -> str | None:
    """Extract the Greenhouse board slug from an apply/absolute URL."""
    if not url:
        return None
    parsed = urlparse(url)
    if "greenhouse.io" not in parsed.netloc:
        return None
    # embed form: ...embed/job_app?for=stripe&token=...
    for_param = parse_qs(parsed.query).get("for")
    if for_param:
        return for_param[0]
    # hosted board: job-boards.greenhouse.io/{slug}/jobs/{id}
    parts = [p for p in parsed.path.split("/") if p and p not in ("embed", "job_app")]
    return parts[0] if parts else None


def parse_questions(raw_questions: list[dict]) -> list[Question]:
    out: list[Question] = []
    for q in raw_questions:
        fields = []
        for f in q.get("fields", []):
            options = [
                Option(label=str(v.get("label", "")), value=str(v.get("value", "")))
                for v in (f.get("values") or [])
            ]
            fields.append(FormField(name=f.get("name", ""), type=f.get("type", ""), options=options))
        out.append(Question(label=q.get("label", ""), required=bool(q.get("required")), fields=fields))
    return out


async def fetch_greenhouse_questions(slug: str, job_id: str) -> list[Question]:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{GH_BASE}/{slug}/jobs/{job_id}", params={"questions": "true"})
        resp.raise_for_status()
        return parse_questions(resp.json().get("questions", []))


# --- planning -----------------------------------------------------------

def _candidate_brief(profile: Profile) -> str:
    prefs = profile.preferences or {}
    bank = profile.answer_bank or {}
    bank_lines = "\n".join(f"- {k}: {v}" for k, v in bank.items()) or "(none)"
    return (
        f"Name: {profile.full_name}\nEmail: {profile.email}\nPhone: {profile.phone}\n"
        f"Work authorization: {prefs.get('work_authorization', '')}\n"
        f"Requires visa sponsorship: {prefs.get('require_sponsorship', False)}\n"
        f"Preferred locations: {', '.join(prefs.get('locations', []))}\n"
        f"Skills: {', '.join(profile.skills)}\n"
        f"Answer bank:\n{bank_lines}\n"
        f"Resume (verbatim):\n{(profile.raw_text or '')[:4000]}"
    )


def _questions_for_prompt(questions: list[Question]) -> str:
    lines = []
    for q in questions:
        for f in q.fields:
            if f.name in ("resume", "cover_letter"):
                continue
            opts = f"  options: {', '.join(o.label for o in f.options)}" if f.options else ""
            req = "REQUIRED" if q.required else "optional"
            lines.append(f"- name={f.name} | type={f.type} | {req} | {q.label}{opts}")
    return "\n".join(lines)


_SYSTEM = (
    "You are completing a job application form for the candidate described below. "
    "Answer each form field using ONLY the candidate's real information. Never invent "
    "facts. For choice questions, the answer MUST be exactly one of the provided options. "
    "For yes/no work-authorization and sponsorship questions, reason from the candidate's "
    "work authorization and sponsorship needs. If a field cannot be answered truthfully, "
    "leave it blank.\n\nCandidate:\n"
)


def plan_answers(questions: list[Question], profile: Profile) -> tuple[AnswerSet, list[str]]:
    """Return (answer set, list of required question labels still unanswered)."""
    answerable = [q for q in questions if any(f.name not in ("resume", "cover_letter") for f in q.fields)]
    if not answerable:
        return AnswerSet(), []

    plan = parse_structured(
        system=_SYSTEM + _candidate_brief(profile),
        user="Fill these fields:\n" + _questions_for_prompt(questions),
        schema=AnswerSet,
        cache_system=False,
        max_tokens=2000,
    )

    by_name = {a.name: a for a in plan.answers}
    unanswered: list[str] = []
    for q in questions:
        if not q.required:
            continue
        for f in q.fields:
            if f.name in ("resume", "cover_letter"):
                continue  # files handled separately
            a = by_name.get(f.name)
            if not _is_answered(f, a):
                unanswered.append(q.label)
                break
    return plan, unanswered


def _is_answered(field: FormField, answer: Answer | None) -> bool:
    if answer is None:
        return False
    if field.type == "multi_value_multi_select":
        allowed = {o.label for o in field.options}
        return bool(answer.values) and all(v in allowed for v in answer.values)
    if field.type == "multi_value_single_select":
        return answer.value in {o.label for o in field.options}
    return bool(answer.value.strip())
