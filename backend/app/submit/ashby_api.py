"""Schema-driven Ashby submitter.

Ashby's hosted job board (jobs.ashbyhq.com) embeds the full application form as
JSON (`window.__appData`) in the page. We fetch that, have the LLM answer every
field from the profile + answer bank, validate coverage, then:

- DRY_RUN  -> return a full preview (status becomes 'queued', like Greenhouse).
- Live     -> route to NeedsHuman: Ashby gates submission behind a reCAPTCHA,
              which we NEVER auto-solve (locked safety rule). The completed
              answer set + tailored docs are handed to the human for the final
              click.

This is a large upgrade over the old "unsupported ATS" path: the agent now reads
the real form and plans every answer (work-auth, LinkedIn, location, custom Qs).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx

from app.config import Settings
from app.llm.answers import Answer, AnswerSet, _candidate_brief
from app.llm.client import parse_structured
from app.models import Job, Profile
from app.submit.base import DryRunFilled, Failed, NeedsHuman, Submitted, SubmitResult

FILE_TYPE = "File"


@dataclass
class AshbyField:
    path: str
    title: str
    type: str
    required: bool
    options: list[str] = field(default_factory=list)
    value_map: dict[str, str] = field(default_factory=dict)


@dataclass
class AshbyForm:
    fields: list[AshbyField]
    org: str = ""
    form_def_id: str = ""


# --- form extraction (pure; testable against a saved fixture) ------------

def _find_first(data: object, key: str):
    """First value for `key` anywhere in a nested dict/list structure."""
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if key in node:
                return node[key]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _extract_app_data(html: str) -> dict:
    m = re.search(r"window\.__appData\s*=\s*", html)
    if not m:
        raise ValueError("no __appData in page")
    start = m.end()
    depth = 0
    for i in range(start, len(html)):
        c = html[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(html[start:i + 1])
    raise ValueError("unterminated __appData JSON")


def extract_form(html: str) -> AshbyForm:
    data = _extract_app_data(html)
    entries = _find_first(data, "fieldEntries") or []
    fields: list[AshbyField] = []
    for e in entries:
        f = e.get("field") or {}
        if not f.get("path"):
            continue
        sv = f.get("selectableValues") or []
        options = [str(o.get("label", "")) for o in sv]
        value_map = {str(o.get("label", "")): str(o.get("value") or o.get("label", "")) for o in sv}
        fields.append(AshbyField(
            path=str(f.get("path")),
            title=str(f.get("title") or f.get("humanReadablePath") or f.get("path")),
            type=str(f.get("type", "")),
            required=bool(e.get("isRequired")),
            options=options,
            value_map=value_map,
        ))
    return AshbyForm(
        fields=fields,
        org=str(_find_first(data, "organizationHostedJobsPageName") or ""),
        form_def_id=str(_find_first(data, "applicationFormDefinitionId") or ""),
    )


def _fetch_html(url: str) -> str:
    with httpx.Client(timeout=25, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True) as c:
        resp = c.get(url)
        resp.raise_for_status()
        return resp.text


# --- answer planning -----------------------------------------------------

_SYSTEM = (
    "You are completing a job application form for the candidate described below. "
    "Answer each field using ONLY the candidate's real information; never invent facts. "
    "Use the field's `name` (its path) as the answer `name`. For choice fields the answer "
    "MUST be exactly one of the provided options. Reason yes/no work-authorization and "
    "sponsorship questions from the candidate's authorization and sponsorship needs. If a "
    "field cannot be answered truthfully, leave it blank.\n\nCandidate:\n"
)


def _fields_for_prompt(fields: list[AshbyField]) -> str:
    lines = []
    for f in fields:
        opts = f"  options: {', '.join(f.options)}" if f.options else ""
        req = "REQUIRED" if f.required else "optional"
        lines.append(f"- name={f.path} | type={f.type} | {req} | {f.title}{opts}")
    return "\n".join(lines)


def _backfill_core(by: dict[str, Answer], fields: list[AshbyField], profile: Profile) -> None:
    """Deterministically fill identity/contact fields the LLM may leave blank."""
    bank = profile.answer_bank or {}
    prefs = profile.preferences or {}

    def put(path: str, value: str) -> None:
        a = by.get(path)
        if value and (a is None or not a.value.strip()):
            by[path] = Answer(name=path, value=value)

    for f in fields:
        title = (f.title or "").lower()
        if f.path == "_systemfield_name":
            put(f.path, profile.full_name or "")
        elif f.type == "Email" or f.path == "_systemfield_email":
            put(f.path, profile.email or "")
        elif f.type == "Phone":
            put(f.path, profile.phone or "")
        elif f.path == "_systemfield_location" or f.type == "Location":
            loc = bank.get("current_location") or ", ".join(prefs.get("locations", [])[:1])
            put(f.path, loc)
        elif "linkedin" in title:
            put(f.path, bank.get("linkedin", ""))
        elif "github" in title:
            put(f.path, bank.get("github", ""))
        elif "portfolio" in title or "website" in title:
            put(f.path, bank.get("portfolio", ""))


def plan_ashby_answers(fields: list[AshbyField], profile: Profile) -> dict[str, Answer]:
    answerable = [f for f in fields if f.type != FILE_TYPE]
    by: dict[str, Answer] = {}
    if answerable:
        plan = parse_structured(
            system=_SYSTEM + _candidate_brief(profile),
            user="Fill these fields:\n" + _fields_for_prompt(answerable),
            schema=AnswerSet,
            cache_system=False,
            max_tokens=1500,
        )
        by = {a.name: a for a in plan.answers}
    _backfill_core(by, fields, profile)
    return by


def _covered(f: AshbyField, by: dict[str, Answer]) -> bool:
    a = by.get(f.path)
    if a is None:
        return False
    if f.options:
        if a.values:
            return all(v in f.options for v in a.values)
        return a.value in f.options
    return bool(a.value.strip())


def _preview(fields: list[AshbyField], by: dict[str, Answer]) -> list[dict]:
    rows = []
    for f in fields:
        if f.type == FILE_TYPE:
            rows.append({"label": f.title, "name": f.path, "type": f.type, "answer": "(tailored resume attached)"})
            continue
        a = by.get(f.path)
        ans = ", ".join(a.values) if (a and a.values) else (a.value if a else "")
        rows.append({"label": f.title, "name": f.path, "type": f.type, "answer": ans})
    return rows


def build_and_submit(
    job: Job, profile: Profile, settings: Settings, *, resume_path: str | None = None
) -> tuple[SubmitResult, dict]:
    """Plan answers and preview (DRY_RUN) or route the captcha step to a human."""
    url = job.apply_url or (job.raw or {}).get("applyUrl", "")
    if not url:
        return Failed("no Ashby apply URL"), {}

    try:
        form = extract_form(_fetch_html(url))
    except (httpx.HTTPError, ValueError) as exc:
        return Failed(f"Ashby form fetch failed: {exc}"), {}

    by = plan_ashby_answers(form.fields, profile)

    resume_available = bool(resume_path or profile.base_resume_path)
    unanswered: list[str] = []
    for f in form.fields:
        if not f.required:
            continue
        if f.type == FILE_TYPE:
            if not resume_available:
                unanswered.append(f.title)
        elif not _covered(f, by):
            unanswered.append(f.title)

    preview = {"answers": _preview(form.fields, by), "unanswered": unanswered}

    if unanswered:
        return NeedsHuman("missing answers: " + "; ".join(unanswered[:5])), preview
    if settings.dry_run:
        return DryRunFilled(), preview
    # Live: Ashby gates submission behind reCAPTCHA — never auto-solved.
    return NeedsHuman(
        "Ashby requires a reCAPTCHA on submit — form is complete; final click routed to you"
    ), preview
