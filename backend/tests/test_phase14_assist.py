"""Assisted co-browsing: fill logic, managed session/queue, live streaming, input.

Playwright runs headless against a file:// fixture only — never a real site. The
fixture mimics Ashby's DOM: inputs whose id/name equal the field *path*, titles in
plain (non-<label>) elements, and a Yes/No choice rendered as buttons.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_assist.db")

from app.submit import assist_session  # noqa: E402
from app.submit.ashby_assist import (  # noqa: E402
    ApplicationAutomationResult,
    ApplicationAutomationState,
    advance_application_step,
    application_control_ready,
    application_form_ready,
    fill_form,
    follow_application_control,
    login_gate,
    open_and_fill,
)

_FORM = """<!doctype html><html><body>
<form onsubmit="return false">
  <div>Legal Name</div>
  <input id="_systemfield_name" name="_systemfield_name" placeholder="Type here...">
  <div>Email</div>
  <input id="_systemfield_email" name="_systemfield_email" type="email" placeholder="hello@example.com">
  <div class="field">
    <div>Are you authorized to work in the US?</div>
    <button type="button" onclick="this.setAttribute('data-selected','1')">Yes</button>
    <button type="button" onclick="this.setAttribute('data-selected','1')">No</button>
  </div>
  <div>Resume</div>
  <input id="_systemfield_resume" type="file">
  <button type="submit">Submit</button>
</form></body></html>"""

_LOGIN_GATE = """<!doctype html><html><body>
<div role="dialog">
  <h2>Sign in to continue</h2>
  <button>Continue with Google</button>
  <button>Sign in with Email</button>
</div>
<input name="candidate_name">
</body></html>"""

# Answer rows as produced by ashby_api._preview: name == field path.
_ANSWERS = [
    {"label": "Legal Name", "name": "_systemfield_name", "type": "String", "answer": "Alex Dev"},
    {"label": "Email", "name": "_systemfield_email", "type": "Email", "answer": "alex@example.com"},
    {"label": "Are you authorized to work in the US?", "name": "q_auth",
     "type": "ValueSelect", "answer": "Yes"},
    {"label": "Resume", "name": "_systemfield_resume", "type": "File", "answer": "(attached)"},
]


def _fixture(tmp_path: Path) -> tuple[str, str]:
    page = tmp_path / "form.html"
    page.write_text(_FORM, encoding="utf-8")
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 dummy")
    return page.as_uri(), str(resume)


def _wait_until(fn, timeout=40) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


# --- fill logic (real, headless, local Ashby-shaped fixture) ------------

def test_fill_by_path_and_choice(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    uri, resume = _fixture(tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(uri, wait_until="load")
        filled, missed = fill_form(page, _ANSWERS, resume)
        # text fields addressed by id/name == path
        assert page.locator("#_systemfield_name").input_value() == "Alex Dev"
        assert page.locator("#_systemfield_email").input_value() == "alex@example.com"
        # Yes/No choice: the "Yes" button was clicked
        assert page.get_by_role("button", name="Yes", exact=True).get_attribute("data-selected") == "1"
        assert page.locator("#_systemfield_resume").input_value().endswith("resume.pdf")
        browser.close()
    assert filled == 4 and missed == []  # resume + name + email + choice


def test_application_automation_advances_safe_steps_and_stops_at_final_submit(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    workflow = tmp_path / "safe-multistep.html"
    workflow.write_text(
        """<!doctype html><html><body>
        <form id="identity">
          <h1>Personal information</h1>
          <label>First name <input name="first_name" required></label>
          <label>Email <input name="email" type="email" required></label>
          <button type="button" onclick="identity.hidden=true;questions.hidden=false">Continue</button>
        </form>
        <form id="questions" hidden>
          <h1>Application questions</h1>
          <label>Why this role? <textarea name="motivation" required></textarea></label>
          <button type="button" onclick="questions.hidden=true;review.hidden=false">Save and continue</button>
        </form>
        <section id="review" hidden>
          <h1>Review your application</h1>
          <button type="button" onclick="document.body.dataset.submitted='true'">Apply</button>
        </section>
        </body></html>""",
        encoding="utf-8",
    )
    answers = [
        {"label": "First name", "name": "first_name", "type": "String", "answer": "Alex"},
        {"label": "Email", "name": "email", "type": "String", "answer": "alex@example.test"},
        {"label": "Why this role?", "name": "motivation", "type": "String", "answer": "A strong match."},
    ]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(workflow.as_uri(), wait_until="load")
        state = ApplicationAutomationState()

        first = advance_application_step(page, answers, None, state)
        assert first.outcome == "progressed"
        assert first.filled == 2
        assert page.locator("#questions").is_visible()

        second = advance_application_step(page, answers, None, state)
        assert second.outcome == "progressed"
        assert second.filled == 1
        assert page.locator("#review").is_visible()

        review = advance_application_step(page, answers, None, state)
        assert review.outcome == "review"
        assert review.missed == ("Review and submit",)
        assert page.locator("body").get_attribute("data-submitted") is None
        browser.close()


def test_application_automation_asks_for_missing_required_answer_before_continue(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    workflow = tmp_path / "required-answer.html"
    workflow.write_text(
        """<!doctype html><html><body><form>
        <label>First name <input name="first_name" required></label>
        <label>Security clearance <select name="clearance" required>
          <option value="">Choose one</option><option>Yes</option><option>No</option>
        </select></label>
        <button type="button" onclick="document.body.dataset.advanced='true'">Continue</button>
        </form></body></html>""",
        encoding="utf-8",
    )
    answers = [
        {"label": "First name", "name": "first_name", "type": "String", "answer": "Alex"},
    ]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(workflow.as_uri(), wait_until="load")

        result = advance_application_step(page, answers, None, ApplicationAutomationState())

        assert result.outcome == "needs_user"
        assert result.missed == ("Security clearance",)
        assert page.locator("body").get_attribute("data-advanced") is None
        browser.close()


def test_deloitte_personal_information_fills_equivalent_visible_labels(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    page_path = tmp_path / "deloitte-personal-information.html"
    page_path.write_text(
        """<!doctype html><html><body>
        <h1>Personal information</h1>
        <label>First name (legal) * <input required></label>
        <label>First name (preferred) * <input required></label>
        <label>Last name * <input required></label>
        <label>Preferred email * <input type="email" required></label>
        <label>Phone number * <input type="tel" required></label>
        <label>Address line 1 * <input required></label>
        <button type="button">Continue</button>
        </body></html>""",
        encoding="utf-8",
    )
    answers = [
        {"label": "First name", "name": "first_name", "type": "String", "answer": "Alex"},
        {"label": "Last name", "name": "last_name", "type": "String", "answer": "Candidate"},
        {"label": "Email", "name": "email", "type": "Email", "answer": "alex@example.com"},
        {"label": "Phone", "name": "phone", "type": "String", "answer": "+12025550123"},
    ]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(page_path.as_uri(), wait_until="load")

        result = advance_application_step(page, answers, None, ApplicationAutomationState())

        assert page.get_by_label("First name (legal)", exact=False).input_value() == "Alex"
        assert page.get_by_label("First name (preferred)", exact=False).input_value() == "Alex"
        assert page.get_by_label("Last name", exact=False).input_value() == "Candidate"
        assert page.get_by_label("Preferred email", exact=False).input_value() == "alex@example.com"
        assert page.get_by_label("Phone number", exact=False).input_value() == "+12025550123"
        assert result.outcome == "needs_user"
        assert result.missed == ("Address line 1",)
        browser.close()


def test_deloitte_prefilled_personal_information_reports_only_unknown_facts(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    page_path = tmp_path / "deloitte-prefilled-personal-information.html"
    page_path.write_text(
        """<!doctype html><html><body>
        <h1>Application Process</h1>
        <nav>Select your resume · Personal information · Job specific questions · Review and submit</nav>
        <label>First name (legal) * <input value="Alex"></label>
        <label>First name (preferred) * <input></label>
        <label>Last name * <input value="Candidate"></label>
        <label>Preferred email * <input type="email" value="alex@example.com"></label>
        <label>Phone number * <input type="tel"></label>
        <label>Address line 1 * <input></label>
        <button type="button">Continue</button>
        </body></html>""",
        encoding="utf-8",
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(page_path.as_uri(), wait_until="load")

        result = advance_application_step(page, [], None, ApplicationAutomationState())

        assert page.get_by_label("First name (preferred)", exact=False).input_value() == "Alex"
        assert result.outcome == "needs_user"
        assert result.missed == ("Phone number", "Address line 1")
        browser.close()


def test_application_automation_hands_visible_captcha_to_user(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    workflow = tmp_path / "captcha.html"
    workflow.write_text(
        """<!doctype html><html><body>
        <div class="g-recaptcha">Verify that you are human</div>
        <button type="button" onclick="document.body.dataset.advanced='true'">Continue</button>
        </body></html>""",
        encoding="utf-8",
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(workflow.as_uri(), wait_until="load")

        result = advance_application_step(page, [], None, ApplicationAutomationState())

        assert result.outcome == "needs_user"
        assert result.missed == ("Complete CAPTCHA or account verification",)
        assert page.locator("body").get_attribute("data-advanced") is None
        browser.close()


def test_job_post_easy_apply_button_opens_application_flow(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    job_post = tmp_path / "linkedin-job.html"
    job_post.write_text(
        """<!doctype html><html><body>
        <h1>Software Engineer</h1>
        <button aria-label="Easy Apply to Software Engineer"
          data-control-name="jobdetails_topcard_inapply"
          onclick="this.dataset.clicked='true';document.getElementById('application').hidden=false">
          Easy Apply
        </button>
        <div id="application" class="jobs-easy-apply-modal" hidden>
          <form>
            <label>Email <input name="email" type="email"></label>
            <button type="submit">Submit application</button>
          </form>
        </div>
        </body></html>""",
        encoding="utf-8",
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(job_post.as_uri(), wait_until="load")

        assert follow_application_control(page, set()) is True
        assert page.get_by_role("button", name="Easy Apply to Software Engineer").get_attribute("data-clicked") == "true"
        assert page.locator("#application").is_visible()
        assert application_form_ready(page) is True
        browser.close()


def test_unrelated_page_link_is_not_an_application_entry_control(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    page_path = tmp_path / "not-an-application.html"
    page_path.write_text('<a href="/help">Help</a>', encoding="utf-8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(page_path.as_uri(), wait_until="load")
        assert application_control_ready(page) is False
        browser.close()


def test_job_detail_apply_now_button_is_application_entry_not_final(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    job_detail = tmp_path / "job-detail.html"
    job_detail.write_text(
        """<!doctype html><html><body>
        <a href="#search">Back to search results</a>
        <h1>Agentic AI Engineer</h1>
        <h2>Position Summary</h2>
        <p>Role description</p>
        <aside><h2>Similar jobs</h2></aside>
        <button type="button" onclick="this.dataset.clicked='true'">Apply now</button>
        </body></html>""",
        encoding="utf-8",
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(job_detail.as_uri(), wait_until="load")

        result = advance_application_step(page, [], None, ApplicationAutomationState())

        assert result.outcome == "progressed"
        assert page.get_by_role("button", name="Apply now").get_attribute("data-clicked") == "true"
        browser.close()


def test_linkedin_job_detail_apply_button_is_application_entry(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    job_detail = tmp_path / "linkedin-detail.html"
    job_detail.write_text(
        """<!doctype html><html><body>
        <h1>Agentic AI Engineer</h1>
        <button type="button" onclick="this.dataset.clicked='true'">Apply</button>
        <section><h2>People you can reach out to</h2></section>
        <section><h2>About the job</h2><p>Healthcare AI role</p></section>
        </body></html>""",
        encoding="utf-8",
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(job_detail.as_uri(), wait_until="load")

        result = advance_application_step(page, [], None, ApplicationAutomationState())

        assert result.outcome == "progressed"
        assert page.get_by_role("button", name="Apply", exact=True).get_attribute("data-clicked") == "true"
        browser.close()


def test_linkedin_external_apply_link_navigates_managed_tab(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    job_detail = tmp_path / "linkedin-external-detail.html"
    job_detail.write_text(
        """<!doctype html><html><body>
        <h1>Agentic AI Engineer</h1>
        <section><h2>People you can reach out to</h2></section>
        <section><h2>About the job</h2><p>Healthcare AI role</p></section>
        <a href="https://employer.test/apply" target="_blank">Apply</a>
        </body></html>""",
        encoding="utf-8",
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.route(
            "https://employer.test/apply",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body="<h1>Employer application</h1><label>Name <input name='name'></label>",
            ),
        )
        page.goto(job_detail.as_uri(), wait_until="load")

        result = advance_application_step(page, [], None, ApplicationAutomationState())

        assert result.outcome == "progressed"
        assert page.url == "https://employer.test/apply"
        assert len(page.context.pages) == 1
        browser.close()


def test_authenticated_job_post_opens_easy_apply_and_fills_form(tmp_path: Path):
    job_post = tmp_path / "linkedin-authenticated.html"
    job_post.write_text(
        """<!doctype html><html><body>
        <h1>Software Engineer</h1>
        <button aria-label="Easy Apply to Software Engineer"
          data-control-name="jobdetails_topcard_inapply"
          onclick="document.getElementById('application').hidden=false">
          Easy Apply
        </button>
        <div id="application" class="jobs-easy-apply-modal" hidden>
          <form onsubmit="document.body.dataset.submitted='true';return false">
            <label>Name <input name="candidate_name"></label>
            <button type="submit">Submit application</button>
          </form>
        </div>
        </body></html>""",
        encoding="utf-8",
    )
    sess = assist_session.AssistSession(
        user_data_dir=str(tmp_path / "linkedin-profile"),
        headless=True,
        keep_open_seconds=30,
        frame_interval=0.05,
    )
    try:
        sess.enqueue(
            9,
            job_post.as_uri(),
            [{"label": "Name", "name": "candidate_name", "type": "String", "answer": "Alex Dev"}],
            None,
        )
        assert _wait_until(
            lambda: sess.snapshot(9).get("stage") == "live"
            and sess.snapshot(9).get("filled") == 1,
            timeout=15,
        ), sess.snapshot(9)
        assert sess.snapshot(9).get("missed") == ["Review and submit"]
        sess.stop_live(9)
        assert _wait_until(lambda: sess.snapshot(9).get("done"))
    finally:
        sess.close()


def test_resume_source_step_chooses_device_and_uploads_prepared_resume(tmp_path: Path):
    portal = tmp_path / "resume-source.html"
    portal.write_text(
        """<!doctype html><html><body>
        <input id="portal-search" type="search" aria-label="Search jobs">
        <h1>Choose any option to start the application process</h1>
        <button type="button" onclick="
          const input = document.createElement('input');
          input.type = 'file';
          input.id = 'resume-upload';
          input.onchange = () => document.getElementById('next-step').hidden = false;
          document.body.appendChild(input);
          input.click();
        ">From Device</button>
        <section id="next-step" hidden>
          <p>Resume ready</p>
          <button type="button" onclick="this.parentElement.hidden=true;document.getElementById('review').hidden=false">
            Continue
          </button>
        </section>
        <section id="review" hidden>
          <h2>Review application</h2>
          <button type="button" onclick="document.body.dataset.submitted='true'">Submit application</button>
        </section>
        </body></html>""",
        encoding="utf-8",
    )
    resume = tmp_path / "prepared-resume.pdf"
    resume.write_bytes(b"%PDF-1.4 prepared")
    sess = assist_session.AssistSession(
        user_data_dir=str(tmp_path / "resume-source-profile"),
        headless=True,
        keep_open_seconds=20,
        frame_interval=0.05,
    )
    try:
        sess.enqueue(10, portal.as_uri(), [], str(resume))
        assert _wait_until(
            lambda: sess.snapshot(10).get("stage") == "live"
            and sess.snapshot(10).get("filled") == 1
            and sess.snapshot(10).get("missed") == ["Review and submit"],
            timeout=8,
        ), sess.snapshot(10)
        sess.stop_live(10)
        assert _wait_until(lambda: sess.snapshot(10).get("done"))
    finally:
        sess.close()


def test_open_pauses_at_login_gate_before_autofill(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    login = tmp_path / "login.html"
    login.write_text(_LOGIN_GATE, encoding="utf-8")
    answers = [{"label": "Name", "name": "candidate_name", "type": "String",
                "answer": "Must not be filled before login"}]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page, filled, missed = open_and_fill(context, login.as_uri(), answers, None)
        assert page.locator('[name="candidate_name"]').input_value() == ""
        assert filled == 0
        assert missed == ["Sign in required"]
        browser.close()


def test_registration_application_with_password_fields_is_not_a_login_wall(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    registration = tmp_path / "registration-application.html"
    registration.write_text(
        """<!doctype html><html><body>
        <h1>Application Process</h1>
        <nav>Personal information · Job specific questions · Review and submit</nav>
        <label>First name (legal) * <input value="Alex"></label>
        <label>First name (preferred) * <input></label>
        <label>Phone number * <input type="tel"></label>
        <label>Address line 1 * <input></label>
        <label>Create password * <input type="password"></label>
        <button type="button">Continue</button>
        </body></html>""",
        encoding="utf-8",
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(registration.as_uri(), wait_until="load")

        assert login_gate(page) is False
        result = advance_application_step(page, [], None, ApplicationAutomationState())

        assert page.get_by_label("First name (preferred)", exact=False).input_value() == "Alex"
        assert result.outcome == "needs_user"
        assert result.missed == ("Phone number", "Address line 1", "Create password")
        browser.close()


def test_managed_session_exposes_login_handoff(tmp_path: Path):
    login = tmp_path / "login.html"
    login.write_text(_LOGIN_GATE, encoding="utf-8")
    frames: list[bytes] = []
    sess = assist_session.AssistSession(
        user_data_dir=str(tmp_path / "login-profile"),
        headless=True,
        keep_open_seconds=30,
        frame_interval=0.05,
    )
    sess.set_frame_sink(7, frames.append)
    try:
        sess.enqueue(7, login.as_uri(), [], None)
        assert _wait_until(lambda: sess.snapshot(7).get("stage") == "login_required")
        assert frames and frames[-1][:2] == b"\xff\xd8"
        sess.stop_live(7)
        assert _wait_until(lambda: sess.snapshot(7).get("done"))
    finally:
        sess.close()


def test_login_completion_follows_apply_popup_and_resumes_autofill(tmp_path: Path):
    application = tmp_path / "application.html"
    application.write_text(
        """<!doctype html><html><body><form>
        <label>Name <input name="candidate_name"></label>
        <label>Why this role? <textarea name="motivation"></textarea></label>
        <button type="submit">Submit application</button>
        </form></body></html>""",
        encoding="utf-8",
    )
    portal = tmp_path / "portal.html"
    portal.write_text(
        f"""<!doctype html><html><body>
        <div role="dialog">
          <h2>Sign in to continue</h2>
          <button id="login" style="position:absolute;left:20px;top:20px;width:200px;height:60px"
            onclick="this.parentElement.remove();document.getElementById('apply').hidden=false">
            Sign in with Email
          </button>
        </div>
        <a id="apply" hidden target="_blank"
          data-tracking-control-name="public_jobs_apply-link-offsite"
          href="{application.as_uri()}">Apply on company site</a>
        </body></html>""",
        encoding="utf-8",
    )
    answers = [
        {"label": "Name", "name": "candidate_name", "type": "String", "answer": "Alex Dev"},
        {"label": "Why this role?", "name": "motivation", "type": "String",
         "answer": "The role matches my verified experience."},
    ]
    sess = assist_session.AssistSession(
        user_data_dir=str(tmp_path / "continuation-profile"),
        headless=True,
        keep_open_seconds=30,
        frame_interval=0.05,
    )
    states: list[str] = []
    original_set = sess._set

    def record_state(app_id: int, **values):
        if values.get("stage"):
            states.append(values["stage"])
        original_set(app_id, **values)

    sess._set = record_state
    try:
        sess.enqueue(8, portal.as_uri(), answers, None)
        assert _wait_until(lambda: sess.snapshot(8).get("stage") == "login_required")
        sess.push_input(8, {"type": "click", "x": 100, "y": 50})
        assert _wait_until(
            lambda: sess.snapshot(8).get("stage") == "live"
            and sess.snapshot(8).get("filled") == 2,
            timeout=30,
        )
        login_index = states.index("login_required")
        continuation = states[login_index:]
        sequence = [continuation.index(stage)
                    for stage in ("login_required", "opening", "filling", "live")]
        assert sequence == sorted(sequence)
        sess.stop_live(8)
        assert _wait_until(lambda: sess.snapshot(8).get("done"))
    finally:
        sess.close()


def test_application_popup_is_not_blocked_by_login_left_in_opener(tmp_path: Path):
    application = tmp_path / "registration.html"
    application.write_text(
        """<!doctype html><html><body><form>
        <label>First name <input name="first_name"></label>
        <label>Email <input name="email" type="email"></label>
        <button type="submit">Submit application</button>
        </form></body></html>""",
        encoding="utf-8",
    )
    login = tmp_path / "login-opener.html"
    login.write_text(
        f"""<!doctype html><html><body>
        <div role="dialog"><h2>Are you already registered?</h2>
          <input type="password">
          <button style="position:absolute;left:20px;top:20px;width:200px;height:60px"
            onclick="window.open('{application.as_uri()}', '_blank')">Create profile!</button>
        </div>
        </body></html>""",
        encoding="utf-8",
    )
    answers = [
        {"label": "First name", "name": "first_name", "type": "String", "answer": "Alex"},
    ]
    sess = assist_session.AssistSession(
        user_data_dir=str(tmp_path / "login-opener-profile"),
        headless=True,
        keep_open_seconds=20,
        frame_interval=0.05,
    )
    try:
        sess.enqueue(13, login.as_uri(), answers, None)
        assert _wait_until(lambda: sess.snapshot(13).get("stage") == "login_required")

        sess.push_input(13, {"type": "click", "x": 100, "y": 50})

        assert _wait_until(
            lambda: sess.snapshot(13).get("stage") == "live"
            and sess.snapshot(13).get("filled") == 1
            and sess.snapshot(13).get("missed") == ["Review and submit"],
            timeout=12,
        ), sess.snapshot(13)
        sess.stop_live(13)
        assert _wait_until(lambda: sess.snapshot(13).get("done"))
    finally:
        sess.close()


def test_apply_input_forwards_mouse_and_keys(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    uri, _ = _fixture(tmp_path)
    sess = assist_session.AssistSession(user_data_dir=str(tmp_path / "p"), headless=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(uri, wait_until="load")
        page.locator("#_systemfield_name").click()  # focus
        sess._apply_input(page, {"type": "text", "text": "Zoe"})
        assert page.locator("#_systemfield_name").input_value() == "Zoe"
        sess._apply_input(page, {"type": "key", "key": "Backspace"})
        assert page.locator("#_systemfield_name").input_value() == "Zo"
        browser.close()


def test_apply_input_navigates_browser_history(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    first = tmp_path / "first.html"
    second = tmp_path / "second.html"
    first.write_text(
        f'<a href="{second.as_uri()}">Continue to sign in</a>',
        encoding="utf-8",
    )
    second.write_text("<h1>Join portal</h1>", encoding="utf-8")
    sess = assist_session.AssistSession(user_data_dir=str(tmp_path / "p"), headless=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(first.as_uri(), wait_until="load")
        page.get_by_role("link", name="Continue to sign in").click()
        assert page.url == second.as_uri()

        sess._apply_input(page, {"type": "history_back"})
        assert page.url == first.as_uri()

        sess._apply_input(page, {"type": "history_forward"})
        assert page.url == second.as_uri()
        browser.close()


def test_apply_input_scrolls_the_remote_page_and_reports_position(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    tall_page = tmp_path / "tall.html"
    tall_page.write_text(
        '<main style="height: 3200px"><h1>Long application</h1></main>',
        encoding="utf-8",
    )
    sess = assist_session.AssistSession(user_data_dir=str(tmp_path / "p"), headless=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(tall_page.as_uri(), wait_until="load")

        _, scroll_max = sess._scroll_metrics(page)
        assert scroll_max > 2000
        sess._apply_input(page, {"type": "scroll_to", "y": 1200})
        scroll_y, updated_max = sess._scroll_metrics(page)

        assert 1190 <= scroll_y <= 1210
        assert updated_max == scroll_max
        browser.close()


def test_live_user_navigation_automatically_rearms_autofill(tmp_path: Path):
    portal = tmp_path / "manual-step.html"
    portal.write_text(
        """<!doctype html><html><body>
        <input id="portal-search" type="search" aria-label="Search jobs">
        <button id="continue" style="position:absolute;left:20px;top:20px;width:200px;height:60px"
          onclick="this.remove();document.getElementById('application').innerHTML=
            '<label>First name <input name=&quot;first_name&quot;></label>' +
            '<label>Email <input name=&quot;email&quot; type=&quot;email&quot;></label>' +
            '<button type=&quot;submit&quot; onclick=&quot;document.body.dataset.submitted=true&quot;>Submit</button>'">
          Continue manually
        </button>
        <form id="application"></form>
        </body></html>""",
        encoding="utf-8",
    )
    answers = [
        {"label": "First name", "name": "first_name", "type": "String", "answer": "Alex"},
    ]
    sess = assist_session.AssistSession(
        user_data_dir=str(tmp_path / "resume-profile"),
        headless=True,
        keep_open_seconds=30,
        frame_interval=0.05,
    )
    try:
        sess.enqueue(11, portal.as_uri(), answers, None)
        assert _wait_until(
            lambda: sess.snapshot(11).get("stage") == "live"
            and sess.snapshot(11).get("missed") == ["Choose the next application step"],
            timeout=20,
        ), sess.snapshot(11)

        sess.push_input(11, {"type": "click", "x": 100, "y": 50})

        assert _wait_until(
            lambda: sess.snapshot(11).get("stage") == "live"
            and sess.snapshot(11).get("filled") == 1
            and sess.snapshot(11).get("missed") == ["Review and submit"],
            timeout=15,
        ), sess.snapshot(11)
        sess.stop_live(11)
        assert _wait_until(lambda: sess.snapshot(11).get("done"))
    finally:
        sess.close()


def test_recent_progress_retries_a_transient_empty_step(tmp_path: Path, monkeypatch):
    page_path = tmp_path / "transition.html"
    page_path.write_text('<input name="search" type="search">', encoding="utf-8")
    outcomes = iter((
        ApplicationAutomationResult("progressed"),
        ApplicationAutomationResult("needs_user", missed=("Choose the next application step",)),
        ApplicationAutomationResult("needs_user", missed=("Phone number",)),
    ))
    calls = 0

    def scripted_step(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return next(outcomes)

    monkeypatch.setattr(assist_session, "advance_application_step", scripted_step)
    sess = assist_session.AssistSession(
        user_data_dir=str(tmp_path / "transition-profile"),
        headless=True,
        keep_open_seconds=20,
        frame_interval=0.05,
    )
    try:
        sess.enqueue(12, page_path.as_uri(), [], None)

        assert _wait_until(
            lambda: sess.snapshot(12).get("stage") == "live"
            and sess.snapshot(12).get("missed") == ["Phone number"],
            timeout=10,
        ), sess.snapshot(12)
        assert calls == 3
        sess.stop_live(12)
        assert _wait_until(lambda: sess.snapshot(12).get("done"))
    finally:
        sess.close()


# --- managed session: sequential + persistent context reuse -------------

def test_session_processes_sequentially_and_reuses_context(tmp_path: Path, monkeypatch):
    uri, resume = _fixture(tmp_path)
    seen = []
    real = assist_session.open_and_fill

    def spy(context, *a, **k):
        seen.append(id(context))
        return real(context, *a, **k)

    monkeypatch.setattr(assist_session, "open_and_fill", spy)
    sess = assist_session.AssistSession(
        user_data_dir=str(tmp_path / "profile"), headless=True, wait_for_user=False)
    try:
        sess.enqueue(1, uri, _ANSWERS, resume)
        sess.enqueue(2, uri, _ANSWERS, resume)
        assert _wait_until(lambda: sess.snapshot(1).get("done") and sess.snapshot(2).get("done"))
        assert sess.snapshot(1)["filled"] == 4 and sess.snapshot(2)["filled"] == 4
        assert len(seen) == 2 and len(set(seen)) == 1  # same persistent context reused
    finally:
        sess.close()


def test_single_active_others_queue(tmp_path: Path):
    uri, resume = _fixture(tmp_path)
    release = threading.Event()
    sess = assist_session.AssistSession(
        user_data_dir=str(tmp_path / "profile"), headless=True, wait_for_user=True)
    sess._serve_live = lambda page, app_id, **_kwargs: release.wait(15)  # user "finishes" on our signal
    try:
        sess.enqueue(1, uri, _ANSWERS, resume)
        sess.enqueue(2, uri, _ANSWERS, resume)
        assert _wait_until(lambda: sess.snapshot(1).get("stage") in {"opening", "filling", "live"})
        assert sess.snapshot(2).get("stage") == "queued"  # single active tab
        release.set()
        assert _wait_until(lambda: sess.snapshot(1).get("done") and sess.snapshot(2).get("done"))
    finally:
        release.set()
        sess.close()


def test_live_streaming_emits_jpeg_frames(tmp_path: Path):
    uri, resume = _fixture(tmp_path)
    frames: list[bytes] = []
    sess = assist_session.AssistSession(
        user_data_dir=str(tmp_path / "profile"), headless=True, wait_for_user=True,
        keep_open_seconds=30, frame_interval=0.05)
    sess.set_frame_sink(1, frames.append)
    try:
        sess.enqueue(1, uri, _ANSWERS, resume)
        assert _wait_until(lambda: len(frames) >= 1, timeout=30)
        assert frames[0][:2] == b"\xff\xd8"  # JPEG magic
        sess.stop_live(1)
        assert _wait_until(lambda: sess.snapshot(1).get("done"))
    finally:
        sess.close()


def test_late_frame_sink_receives_latest_browser_frame(tmp_path: Path):
    class Page:
        def screenshot(self, **_kwargs):
            return b"latest-frame"

    sess = assist_session.AssistSession(
        user_data_dir=str(tmp_path / "profile"), headless=True)
    sess.emit_frame("execution-1", Page())

    frames: list[bytes] = []
    sess.set_frame_sink("execution-1", frames.append)

    assert frames == [b"latest-frame"]


# The /intervention/{id}/live HTML endpoint that enqueued assist sessions was
# removed with the server-rendered dashboard. The assist backend (session queue,
# fill logic, live streaming) is still covered by the tests above; a React
# intervention UI would drive it through a future /api endpoint.
