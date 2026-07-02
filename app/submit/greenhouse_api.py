"""Schema-driven Greenhouse submitter.

Fetches the job's question schema, has the LLM answer every field from the
profile + answer bank, validates coverage, then either previews (DRY_RUN) or
attempts the application POST. If the API rejects the submission (it may require
the company's board key), it falls back to NeedsHuman rather than failing loudly.
"""
from __future__ import annotations

import httpx

from app.config import Settings
from app.llm.answers import (
    GH_BASE,
    Question,
    board_slug_from_url,
    parse_questions,
    plan_answers,
)
from app.models import Job, Profile
from app.submit.base import DryRunFilled, Failed, NeedsHuman, Submitted, SubmitResult


def _fetch_questions_sync(slug: str, job_id: str) -> list[Question]:
    with httpx.Client(timeout=20) as client:
        resp = client.get(f"{GH_BASE}/{slug}/jobs/{job_id}", params={"questions": "true"})
        resp.raise_for_status()
        return parse_questions(resp.json().get("questions", []))


def _preview(questions: list[Question], by_name: dict) -> list[dict]:
    rows = []
    for q in questions:
        for f in q.fields:
            if f.name in ("resume", "cover_letter"):
                continue
            a = by_name.get(f.name)
            ans = ""
            if a is not None:
                ans = ", ".join(a.values) if a.values else a.value
            rows.append({"label": q.label, "name": f.name, "type": f.type, "answer": ans})
    return rows


def _build_post_data(questions: list[Question], by_name: dict, profile: Profile) -> list[tuple[str, str]]:
    """Assemble multipart form fields, mapping select labels -> option value ids."""
    data: list[tuple[str, str]] = []
    for q in questions:
        for f in q.fields:
            if f.name in ("resume", "cover_letter"):
                continue
            a = by_name.get(f.name)
            if a is None:
                continue
            label_to_value = {o.label: (o.value or o.label) for o in f.options}
            if f.type == "multi_value_multi_select":
                for v in a.values:
                    data.append((f.name, label_to_value.get(v, v)))
            elif f.type == "multi_value_single_select":
                if a.value:
                    data.append((f.name, label_to_value.get(a.value, a.value)))
            elif a.value.strip():
                data.append((f.name, a.value))
    return data


def build_and_submit(job: Job, profile: Profile, settings: Settings) -> tuple[SubmitResult, dict]:
    """Plan answers and submit (or preview). Returns (result, preview_dict)."""
    url = job.apply_url or (job.raw or {}).get("absolute_url", "")
    slug = board_slug_from_url(url)
    if not slug:
        return Failed("could not determine Greenhouse board slug"), {}

    try:
        questions = _fetch_questions_sync(slug, job.source_job_id)
    except httpx.HTTPError as exc:
        return Failed(f"schema fetch failed: {exc}"), {}

    plan, unanswered = plan_answers(questions, profile)
    by_name = {a.name: a for a in plan.answers}
    preview = {"answers": _preview(questions, by_name), "unanswered": unanswered}

    if unanswered:
        return NeedsHuman("missing answers: " + "; ".join(unanswered[:5])), preview

    if settings.dry_run:
        return DryRunFilled(), preview

    # Live submission attempt.
    data = _build_post_data(questions, by_name, profile)
    data += [
        ("first_name", (profile.full_name or "").split(" ")[0]),
        ("last_name", " ".join((profile.full_name or "").split(" ")[1:])),
        ("email", profile.email),
        ("phone", profile.phone),
    ]
    files = {}
    if profile.base_resume_path:
        try:
            files["resume"] = open(profile.base_resume_path, "rb")
        except OSError:
            pass
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(f"{GH_BASE}/{slug}/jobs/{job.source_job_id}", data=data, files=files)
    finally:
        for fh in files.values():
            fh.close()

    if resp.status_code in (200, 201):
        return Submitted(), preview
    if resp.status_code in (401, 403):
        return NeedsHuman("Greenhouse requires the company's board key to submit via API"), preview
    return Failed(f"greenhouse submit {resp.status_code}: {resp.text[:200]}"), preview
