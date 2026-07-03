"""Schema-driven Ashby submitter — form extraction, planning, DRY_RUN, captcha route.

All tests run against an in-repo HTML fixture (a captured `window.__appData`
blob), never a live Ashby site.
"""
from __future__ import annotations

import json

from app.config import Settings
from app.llm.answers import Answer, AnswerSet
from app.models import Job, Profile
from app.submit import ashby_api
from app.submit.base import DryRunFilled, NeedsHuman


def _field(path, title, ftype, required=True, options=None):
    f = {"path": path, "title": title, "type": ftype, "__autoSerializationID": ftype + "Field"}
    if options:
        f["selectableValues"] = [{"label": o, "value": o.lower()} for o in options]
    return {"id": f"def__{path}", "field": f, "isRequired": required}


def _fixture_html() -> str:
    app_data = {
        "posting": {
            "applicationFormDefinitionId": "def-123",
            "organizationHostedJobsPageName": "ramp",
            "form": {"formRender": {"sections": [{"fieldEntries": [
                _field("_systemfield_name", "Legal Name", "String"),
                _field("_systemfield_email", "Email", "Email"),
                _field("q1", "Are you authorized to work in the US?", "ValueSelect", options=["Yes", "No"]),
                _field("q2", "LinkedIn Profile", "String"),
                _field("_systemfield_resume", "Resume", "File"),
            ]}]}},
        }
    }
    return "<html><head><script>window.__appData = " + json.dumps(app_data) + ";</script></head></html>"


def _job() -> Job:
    return Job(source="ashby", source_job_id="j1", company="Ramp", title="AI Engineer",
               dedup_hash="h1", ats_type="ashby", apply_url="https://jobs.ashbyhq.com/ramp/j1/application")


def _profile(bank=None) -> Profile:
    return Profile(full_name="Alex Dev", email="alex@example.com", phone="+1 555 0100",
                   skills=["python"], answer_bank=bank if bank is not None else {"linkedin": "https://linkedin.com/in/alex"},
                   preferences={"locations": ["Remote"]}, base_resume_path="/tmp/resume.pdf")


def test_extract_form_parses_fields():
    form = ashby_api.extract_form(_fixture_html())
    assert form.org == "ramp"
    assert [f.path for f in form.fields] == [
        "_systemfield_name", "_systemfield_email", "q1", "q2", "_systemfield_resume"]
    q1 = next(f for f in form.fields if f.path == "q1")
    assert q1.type == "ValueSelect" and q1.options == ["Yes", "No"]
    resume = next(f for f in form.fields if f.path == "_systemfield_resume")
    assert resume.type == "File" and resume.required


def _stub_llm(monkeypatch, answers):
    monkeypatch.setattr(ashby_api, "_fetch_html", lambda url: _fixture_html())
    monkeypatch.setattr(ashby_api, "parse_structured", lambda **kw: AnswerSet(answers=answers))


def test_dry_run_fills_and_previews(monkeypatch):
    _stub_llm(monkeypatch, [Answer(name="q1", value="Yes")])  # name/email/linkedin backfilled
    result, preview = ashby_api.build_and_submit(_job(), _profile(), Settings(dry_run=True))
    assert isinstance(result, DryRunFilled)
    assert not preview["unanswered"]
    by = {r["name"]: r["answer"] for r in preview["answers"]}
    assert by["_systemfield_name"] == "Alex Dev"
    assert by["q1"] == "Yes"
    assert by["q2"] == "https://linkedin.com/in/alex"  # backfilled from answer bank
    assert by["_systemfield_resume"] == "(tailored resume attached)"


def test_live_submit_routes_to_human_for_captcha(monkeypatch):
    _stub_llm(monkeypatch, [Answer(name="q1", value="Yes")])
    result, _ = ashby_api.build_and_submit(_job(), _profile(), Settings(dry_run=False))
    assert isinstance(result, NeedsHuman)
    assert "captcha" in result.reason.lower()


def test_missing_required_answer_routes_to_human(monkeypatch):
    # No LinkedIn in the answer bank and the LLM doesn't answer q2 -> uncovered.
    _stub_llm(monkeypatch, [Answer(name="q1", value="Yes")])
    result, preview = ashby_api.build_and_submit(_job(), _profile(bank={}), Settings(dry_run=True))
    assert isinstance(result, NeedsHuman)
    assert "LinkedIn Profile" in result.reason
    assert "LinkedIn Profile" in preview["unanswered"]
