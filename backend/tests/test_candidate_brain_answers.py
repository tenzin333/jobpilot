from app.candidate_brain.services.application_answers import (
    AnswerDraft,
    GroundingCheck,
    GroundingReport,
    ProposedAnswer,
    ProposedAnswerSet,
    resolve_application_answers,
)
from app.candidate_brain.models import Basic, Candidate, Project, Skills
from app.ghost_cursor.models import Action, Target, Workflow
from app.models import Job, Profile
from app.pipeline.fixture_workflows import workflow


def _profile() -> Profile:
    return Profile(
        full_name="Alex Candidate",
        raw_text="Built a deployment platform that reduced release time by 30%.\nPython and AWS.",
        skills=["Python", "AWS"],
        experience=[{"company": "Acme", "title": "Platform Engineer",
                     "bullets": ["Built a deployment platform that reduced release time by 30%."]}],
    )


def _job() -> Job:
    return Job(title="Platform Engineer", company="Northstar", location="Remote",
               description="Build reliable deployment tooling with Python and AWS.", dedup_hash="brain-job")


def test_generates_and_verifies_cv_grounded_narrative_answer():
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        if kwargs["schema"] is ProposedAnswerSet:
            return ProposedAnswerSet(answers=[ProposedAnswer(
                key="interest",
                answer="I am interested because the role aligns with my Python, AWS, and deployment-platform experience.",
                evidence_ids=["CV1", "CV3", "JOB1"],
            )])
        return GroundingReport(checks=[GroundingCheck(
            key="interest", supported=True, unsupported_claims=[])])

    result = resolve_application_answers(
        _profile(), _job(), workflow("basic", "http://127.0.0.1/test-ats/basic"),
        {"first_name": "Alex"}, complete=complete)

    assert "deployment-platform experience" in result.answers["interest"]
    assert result.evidence["interest"][:2] == ["CV1", "CV3"]
    assert result.drafts == [AnswerDraft(
        field="interest",
        question="Why are you interested?",
        evidence_ids=["CV1", "CV3", "JOB1"],
        evidence_snippets=["Skills: Python, AWS", "Platform Engineer at Acme: Built a deployment platform that reduced release time by 30%."],
    )]
    assert result.unresolved == []
    assert len(calls) == 2
    assert "Northstar" in calls[0]["user"]


def test_approved_structured_candidate_is_used_as_answer_evidence():
    approved = Candidate(
        profile=Basic(
            full_name="Alex Candidate",
            professional_summary="Builds reliable internal developer platforms.",
        ),
        projects=[Project(
            name="Release Console",
            description="A self-service deployment workflow.",
            contributions=["Designed the release approval flow."],
        )],
        skills=Skills(programming_languages=["Python"]),
    )
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        if kwargs["schema"] is ProposedAnswerSet:
            return ProposedAnswerSet(answers=[ProposedAnswer(
                key="interest",
                answer="I am interested because the role aligns with my internal-platform work.",
                evidence_ids=["CV1"],
            )])
        return GroundingReport(checks=[GroundingCheck(
            key="interest", supported=True, unsupported_claims=[])])

    profile = Profile(full_name="Alex Candidate")
    result = resolve_application_answers(
        profile,
        _job(),
        workflow("basic", "http://127.0.0.1/test-ats/basic"),
        {},
        candidate=approved,
        complete=complete,
    )

    assert result.answers["interest"].startswith("I am interested")
    assert result.drafts[0].evidence_snippets == ["Builds reliable internal developer platforms."]
    assert "Release Console" in calls[0]["user"]
    assert "Python" in calls[0]["user"]


def test_rejects_invented_metric_even_when_reviewer_accepts_it():
    def complete(**kwargs):
        if kwargs["schema"] is ProposedAnswerSet:
            return ProposedAnswerSet(answers=[ProposedAnswer(
                key="interest", answer="I improved deployment speed by 95%.", evidence_ids=["CV1"])])
        return GroundingReport(checks=[GroundingCheck(
            key="interest", supported=True, unsupported_claims=[])])

    result = resolve_application_answers(
        _profile(), _job(), workflow("basic", "http://127.0.0.1/test-ats/basic"),
        {}, complete=complete)

    assert result.answers == {}
    assert result.unresolved == ["interest"]


def test_rejects_answer_when_grounding_review_finds_an_unsupported_claim():
    def complete(**kwargs):
        if kwargs["schema"] is ProposedAnswerSet:
            return ProposedAnswerSet(answers=[ProposedAnswer(
                key="interest", answer="I led an AWS platform team.", evidence_ids=["CV1"])])
        return GroundingReport(checks=[GroundingCheck(
            key="interest", supported=False, unsupported_claims=["Led a platform team"])])

    result = resolve_application_answers(
        _profile(), _job(), workflow("basic", "http://127.0.0.1/test-ats/basic"),
        {}, complete=complete)

    assert result.answers == {}
    assert result.unresolved == ["interest"]


def test_sensitive_narrative_question_is_never_sent_to_model():
    sensitive = Workflow(name="sensitive", url="http://127.0.0.1/form", steps=[
        Action(id="visa", kind="fill", binding="visa_explanation",
               target=Target(names=["visa_explanation"], labels=["Explain your visa sponsorship needs"])),
        Action(id="submit", kind="final_submit", target=Target(role="button", labels=["Submit"])),
    ])

    def must_not_run(**_kwargs):
        raise AssertionError("Sensitive questions must not reach the generator")

    result = resolve_application_answers(_profile(), _job(), sensitive, {}, complete=must_not_run)

    assert result.model_dump() == {"answers": {}, "evidence": {}, "drafts": [], "unresolved": []}


def test_existing_saved_answer_wins_without_generation():
    def must_not_run(**_kwargs):
        raise AssertionError("Existing answers must not be regenerated")

    result = resolve_application_answers(
        _profile(), _job(), workflow("basic", "http://127.0.0.1/test-ats/basic"),
        {"interest": "My approved answer."}, complete=must_not_run)

    assert result.answers == {}


def test_contact_details_are_not_exposed_as_review_evidence():
    profile = _profile()
    profile.raw_text += "\nalex@example.com"

    def complete(**kwargs):
        if kwargs["schema"] is ProposedAnswerSet:
            return ProposedAnswerSet(answers=[ProposedAnswer(
                key="interest", answer="I am interested in this role.", evidence_ids=["CV6"])])
        raise AssertionError("An answer with no display-safe evidence must be rejected before review")

    result = resolve_application_answers(
        profile, _job(), workflow("basic", "http://127.0.0.1/test-ats/basic"),
        {}, complete=complete)

    assert result.answers == {}
    assert result.drafts == []
    assert result.unresolved == ["interest"]
