from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.ghost_cursor.models import Action, Confirmation, Policy, State, Workflow, validate_transition
from app.ghost_cursor.runtime import NeedsHuman, Runner, observe
from app.pipeline.fixture_workflows import profile_bindings, workflow
from app.submit.test_ats import validate_base_url
from tests.local_fixtures import browser_page, candidate, local_ats_origin


def runner_for(page, origin, scenario, bindings):
    execution_id = str(uuid4())
    path = f"/test-ats/{scenario}"
    return Runner(page, workflow(scenario, f"{origin}{path}?execution_id={execution_id}"), bindings,
                  Policy(origin=origin, paths=[path]), execution_id)


@pytest.mark.parametrize("scenario", ["basic", "multistep", "weird-ui", "dynamic"])
def test_review_then_exactly_one_correlated_receipt(browser_page, local_ats_origin, candidate, scenario):
    runner = runner_for(browser_page, local_ats_origin, scenario, candidate)
    result = runner.run()
    assert result.state == State.READY_FOR_REVIEW, result
    assert browser_page.evaluate("sessionStorage.getItem('test-ats:last-submission')") is None
    assert all(t.verified for t in runner.trace)
    assert "alex@example.com" not in str([t.model_dump() for t in runner.trace])
    with pytest.raises(ValueError):
        runner.confirm(Confirmation(review_version=1), permitted=False)
    with pytest.raises(ValueError):
        runner.confirm(Confirmation(review_version=2), permitted=True)
    receipt = runner.confirm(Confirmation(review_version=1), permitted=True)
    assert receipt["execution_id"] == runner.execution_id
    assert "values" not in receipt
    with pytest.raises(ValueError):
        runner.confirm(Confirmation(review_version=1), permitted=True)


def test_runner_emits_live_progress_after_verified_actions(browser_page, local_ats_origin, candidate):
    snapshots = []
    execution_id = str(uuid4())
    path = "/test-ats/basic"
    runner = Runner(browser_page, workflow("basic", f"{local_ats_origin}{path}?execution_id={execution_id}"),
        candidate, Policy(origin=local_ats_origin, paths=[path]), execution_id,
        progress=lambda step: snapshots.append((step,
            browser_page.locator('[name="first_name"]').input_value()
            if browser_page.locator('[name="first_name"]').count() else "")))

    assert runner.run().state == State.READY_FOR_REVIEW
    assert snapshots[0][0] == "opened"
    assert ("first_name", "Alex") in snapshots
    assert snapshots[-1][0] == "review"


def test_missing_answer_resumes_same_execution(browser_page, local_ats_origin, candidate):
    candidate.pop("authorized")
    runner = runner_for(browser_page, local_ats_origin, "multistep", candidate)
    result = runner.run()
    assert result.state == State.NEEDS_HUMAN
    assert result.unresolved_fields == ["authorized"]
    assert runner.resume({"authorized": "No"}).state == State.READY_FOR_REVIEW
    assert runner.expected["first_name"] == "Alex"
    assert runner.expected["authorized"] == "No"


def test_review_tamper_is_rejected(browser_page, local_ats_origin, candidate):
    runner = runner_for(browser_page, local_ats_origin, "basic", candidate)
    runner.run()
    browser_page.locator('[data-testid="review-stage"] dd').first.evaluate("e => e.textContent = 'Changed'")
    with pytest.raises(NeedsHuman):
        runner.confirm(Confirmation(review_version=1), permitted=True)
    assert browser_page.evaluate("sessionStorage.getItem('test-ats:last-submission')") is None


def test_invalid_email_requests_specific_correction(browser_page, local_ats_origin, candidate):
    candidate["email"] = "not-an-email"
    runner = runner_for(browser_page, local_ats_origin, "basic", candidate)
    result = runner.run()
    assert result.state == State.NEEDS_HUMAN
    assert result.unresolved_fields == ["email"]
    assert runner.resume({"email": "corrected@example.com"}).state == State.READY_FOR_REVIEW


def test_invalid_radio_answer_requests_correction_without_empty_target_choices(
        browser_page, local_ats_origin, candidate):
    candidate["authorized"] = "True"
    runner = runner_for(browser_page, local_ats_origin, "multistep", candidate)
    result = runner.run()
    assert result.state == State.NEEDS_HUMAN
    assert result.reason_code == "invalid_choice"
    assert result.unresolved_fields == ["authorized"]
    assert runner.target_choices == {}
    assert runner.resume({"authorized": "Yes"}).state == State.READY_FOR_REVIEW


def test_explicit_boolean_answers_are_normalized_for_yes_no_controls():
    profile = SimpleNamespace(answer_bank={"authorized": True, "sponsorship": False},
        first_name="Alex", last_name="Candidate", email="alex@example.com", phone="+12025550123")
    bindings = profile_bindings(profile, SimpleNamespace(resume_path="resume.pdf"))
    assert bindings["authorized"] == "Yes"
    assert bindings["sponsorship"] == "No"


def test_profile_bindings_split_existing_full_name_when_parts_are_missing():
    profile = SimpleNamespace(answer_bank={}, full_name="Alex Morgan Candidate",
        first_name="", last_name="", email="alex@example.com", phone="+12025550123")
    bindings = profile_bindings(profile, SimpleNamespace(resume_path="resume.pdf"))
    assert bindings["first_name"] == "Alex"
    assert bindings["last_name"] == "Candidate"


def test_profile_bindings_map_saved_answer_aliases_without_inference():
    profile = SimpleNamespace(answer_bank={"authorized to work": "Yes", "require sponsorship": "No",
        "linkedin": "https://example.com/profile"}, full_name="Alex Candidate",
        first_name="", last_name="", email="alex@example.com", phone="+12025550123")
    bindings = profile_bindings(profile, SimpleNamespace(resume_path="resume.pdf"))
    assert bindings["authorized"] == "Yes"
    assert bindings["work_authorization"] == "Authorized to work"
    assert bindings["sponsorship"] == "No"
    assert bindings["linkedin_url"] == "https://example.com/profile"


def test_profile_bindings_normalize_a_saved_linkedin_domain():
    profile = SimpleNamespace(answer_bank={"linkedin": "linkedin.com/in/alex-candidate"},
        full_name="Alex Candidate", first_name="", last_name="", email="alex@example.com", phone="555")

    bindings = profile_bindings(profile, SimpleNamespace(resume_path="resume.pdf"))

    assert bindings["linkedin_url"] == "https://linkedin.com/in/alex-candidate"


def test_ambiguous_target_requires_bounded_user_selection(browser_page, local_ats_origin, candidate):
    browser_page.add_init_script("""new MutationObserver(() => {
      const original = document.querySelector('#interest');
      if (original && !document.querySelector('#interest-copy')) {
        const copy = original.cloneNode(true); copy.id = 'interest-copy';
        original.after(copy);
      }
    }).observe(document, {childList: true, subtree: true});""")
    runner = runner_for(browser_page, local_ats_origin, "basic", candidate)
    result = runner.run()
    assert result.state == State.NEEDS_HUMAN
    assert result.reason_code == "target_missing_or_ambiguous"
    choices = runner.target_choices["interest"]
    assert len(choices) == 2
    assert runner.resume({}, {"interest": choices[1]["token"]}).state == State.READY_FOR_REVIEW


def test_policy_and_contract_rejection():
    for origin in ["https://example.com:3000", "http://localhost.evil:3000", "http://user@localhost:3000", "http://localhost:3000/path"]:
        with pytest.raises(ValueError):
            validate_base_url(origin)
    policy = Policy(origin="http://127.0.0.1:3000", paths=["/test-ats/basic"])
    assert not policy.allows("http://127.0.0.1:3001/test-ats/basic")
    assert not policy.allows("http://127.0.0.1:3000/test-ats/dynamic")
    assert not policy.allows("https://example.com/file.js", navigation=False)
    with pytest.raises(ValueError):
        validate_transition("succeeded", "filling")
    with pytest.raises(ValueError):
        validate_transition("queued", "confirming")
    with pytest.raises(ValueError):
        Workflow(name="bad", url="http://localhost:3000", steps=[Action(id="click", kind="click")])
