"""Schema-driven answering: slug parsing, schema parse, planner coverage, submit."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.config import Settings  # noqa: E402
from app.llm import answers as answers_mod  # noqa: E402
from app.llm.answers import (  # noqa: E402
    Answer,
    AnswerSet,
    FormField,
    Option,
    Question,
    board_slug_from_url,
    parse_questions,
    plan_answers,
)
from app.models import Job, Profile  # noqa: E402
from app.submit import greenhouse_api as gha  # noqa: E402
from app.submit.base import DryRunFilled, NeedsHuman


def test_board_slug_from_url():
    assert board_slug_from_url("https://job-boards.greenhouse.io/stripe/jobs/77") == "stripe"
    assert board_slug_from_url("https://boards.greenhouse.io/airbnb/jobs/9") == "airbnb"
    assert board_slug_from_url("https://boards.greenhouse.io/embed/job_app?for=acme&token=1") == "acme"
    assert board_slug_from_url("https://jobs.lever.co/acme/1") is None


def test_parse_questions():
    raw = [
        {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
        {"label": "Country", "required": True, "fields": [
            {"name": "q1", "type": "multi_value_single_select",
             "values": [{"label": "Canada", "value": "1"}, {"label": "India", "value": "2"}]}]},
    ]
    qs = parse_questions(raw)
    assert len(qs) == 2
    assert qs[1].fields[0].options[1].label == "India"
    assert qs[1].fields[0].options[1].value == "2"


def _profile() -> Profile:
    return Profile(full_name="Jane Doe", email="j@x.com", phone="555",
                   preferences={"work_authorization": "India", "require_sponsorship": True},
                   answer_bank={"linkedin": "x"})


def _questions() -> list[Question]:
    return [
        Question(label="Employer", required=True, fields=[FormField(name="q_emp", type="input_text")]),
        Question(label="Authorized?", required=True, fields=[FormField(
            name="q_auth", type="multi_value_single_select",
            options=[Option(label="Yes", value="1"), Option(label="No", value="2")])]),
    ]


def test_plan_answers_full_coverage(monkeypatch):
    monkeypatch.setattr(answers_mod, "parse_structured", lambda **kw: AnswerSet(answers=[
        Answer(name="q_emp", value="Acme"),
        Answer(name="q_auth", value="Yes"),
    ]))
    plan, unanswered = plan_answers(_questions(), _profile())
    assert unanswered == []
    assert {a.name for a in plan.answers} == {"q_emp", "q_auth"}


def test_plan_answers_missing_required(monkeypatch):
    # Missing q_auth, and q_emp blank -> both required flagged.
    monkeypatch.setattr(answers_mod, "parse_structured", lambda **kw: AnswerSet(answers=[
        Answer(name="q_emp", value=""),
        Answer(name="q_auth", value="Maybe"),  # not a valid option
    ]))
    _, unanswered = plan_answers(_questions(), _profile())
    assert "Employer" in unanswered
    assert "Authorized?" in unanswered


def _job() -> Job:
    return Job(source="greenhouse", source_job_id="77", company="Acme", title="AI Engineer",
               dedup_hash="h", ats_type="greenhouse",
               apply_url="https://job-boards.greenhouse.io/acme/jobs/77")


def test_build_and_submit_dry_run(monkeypatch):
    monkeypatch.setattr(gha, "_fetch_questions_sync", lambda slug, jid: _questions())
    monkeypatch.setattr(gha, "plan_answers", lambda qs, prof: (
        AnswerSet(answers=[Answer(name="q_emp", value="Acme"), Answer(name="q_auth", value="Yes")]),
        [],
    ))
    result, preview = gha.build_and_submit(_job(), _profile(), Settings(dry_run=True))
    assert isinstance(result, DryRunFilled)
    assert len(preview["answers"]) == 2
    assert preview["unanswered"] == []


def test_build_and_submit_missing_required_needs_human(monkeypatch):
    monkeypatch.setattr(gha, "_fetch_questions_sync", lambda slug, jid: _questions())
    monkeypatch.setattr(gha, "plan_answers", lambda qs, prof: (AnswerSet(), ["Employer"]))
    result, preview = gha.build_and_submit(_job(), _profile(), Settings(dry_run=True))
    assert isinstance(result, NeedsHuman)
    assert "Employer" in result.reason
