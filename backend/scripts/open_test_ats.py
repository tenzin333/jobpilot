from playwright.sync_api import Page, Playwright, expect, sync_playwright
from app.ghost_cursor.models import ExecutionResult, State

ats_page = "http://localhost:3000/test-ats/basic"

def run(playwright: Playwright, text_fields: dict[str, str]) -> ExecutionResult:
    browser = playwright.chromium.launch(headless=False)
    try:
        page = browser.new_page()
        result = fill_to_review(page, text_fields)
        input("Press Enter to close...")
        return result
    finally:
        browser.close()
        print("Browser closed...")


def fill_to_review(page: Page, text_fields: dict[str, str]) -> ExecutionResult:
    page.goto(ats_page)
    expect(page.get_by_role("heading", name="Candidate details")).to_be_visible()
    print("PASS: basic fixture opened")

    fill_basic_form(page, text_fields)
    expect(page.get_by_test_id("review-stage")).to_have_count(0)
    expect(page.get_by_test_id("submission-receipt")).to_have_count(0)
    assert page.evaluate("sessionStorage.getItem('test-ats:last-submission')") is None
    print("PASS: form filled; no review or submission performed")
    review_button = page.get_by_role("button", name="Review application")
    review_button.click()
    
    
    review = page.get_by_test_id("review-stage")
    expect(review).to_be_visible()
    expect(review.get_by_text(text_fields["First Name"], exact=True)).to_be_visible()
    expect(review.get_by_text("test-resume.txt", exact=True)).to_be_visible()

    expect(page.get_by_test_id("submission-receipt")).to_have_count(0)
    assert page.evaluate("sessionStorage.getItem('test-ats:last-submission')") is None
    print("PASS: review ready; name and resume verified; nothing submitted")
    
    return ExecutionResult(
        execution_id="test-run-1",
        state=State.READY_FOR_REVIEW,
        reason_code="review_checks_passed"
    )


def render_browser(text_fields: dict[str, str]) -> ExecutionResult:
    with sync_playwright() as playwright:
        return run(playwright, text_fields)


def fill_basic_form(page: Page, text_fields: dict[str, str]):
    
    for label, value in text_fields.items():
        field = page.get_by_label(label)
        field.fill(value)
        expect(field).to_have_value(value)
        print(f"PASS: {label} filled")

    # Dropdowns use select_option instead of fill.
    authorization = page.get_by_label("Work authorization")
    authorization.select_option(label="Authorized to work")
    expect(authorization).to_have_value("Authorized to work")
    print("PASS: Work authorization selected")

    # The fixture accepts a text file; keep the test document entirely in memory.
    resume = page.get_by_label("Resume")
    resume.set_input_files({
        "name": "test-resume.txt",
        "mimeType": "text/plain",
        "buffer": b"Test Candidate\nSynthetic resume for the local ATS exercise only.\n",
    })
    assert resume.evaluate("input => input.files[0].name") == "test-resume.txt"
    print("PASS: Resume uploaded")

if __name__ == "__main__":
    # Synthetic values for this local exercise, not real candidate information.
    text_fields = {
        "First Name": "Alex",
        "Last Name": "Candidate",
        "Email Address": "test.candidate@example.com",
        "Phone": "+12025550123",
        "Why are you interested?": "I am testing this local application form.",
    }
    result = render_browser(text_fields)
    print(result.model_dump(mode="json"))
