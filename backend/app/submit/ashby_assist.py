"""Form-fill primitives for the assisted hand-off (captcha-gated apps: Ashby, etc.).

`fill_form` fills a page's fields from planned answers + uploads the résumé;
`open_and_fill` opens a new tab in an existing (persistent) context and fills it.
The actual browser lifecycle — one persistent, visible Chromium processing a job
queue one tab at a time — lives in `assist_session.py`. We NEVER auto-solve the
captcha or auto-click submit (locked safety rule); the human owns those two steps.

These primitives are pure enough to test against a local file:// fixture in
headless mode (never a real site).
"""
from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

log = logging.getLogger("assist")

_LOGIN_DIALOG_SELECTORS = (
    '[role="dialog"]:visible',
    '[aria-modal="true"]:visible',
    '.contextual-sign-in-modal:visible',
)
_LOGIN_WORDS = ("sign in", "log in", "login", "continue with google", "continue with email")
_APPLY_CONTROL_SELECTORS = (
    # LinkedIn Easy Apply opens an application modal from the job post. Keep
    # these selectors specific so a form's final Apply/Submit button can never
    # be mistaken for navigation.
    'button[data-control-name="jobdetails_topcard_inapply"]:visible',
    'button.jobs-apply-button:visible',
    'button[aria-label*="Easy Apply"]:visible',
    'a[data-tracking-control-name*="apply-link-offsite"]:visible',
    'a[data-tracking-control-name*="apply"][href]:visible',
    'a[data-testid*="apply"][href]:visible',
)
_GENERIC_APPLY_CONTROL_SELECTORS = (
    "button:visible",
    '[role="button"]:visible',
    'a[href]:visible',
)
_JOB_ENTRY_LABELS = {"apply", "apply now", "apply for this job", "apply to this job"}
_JOB_DETAIL_CUES = (
    "about the job",
    "back to search results",
    "job description",
    "job details",
    "people you can reach out to",
    "position summary",
    "similar jobs",
)
_RESUME_DEVICE_SELECTORS = (
    'button:has-text("From Device"):visible',
    'a:has-text("From Device"):visible',
    '[role="button"]:has-text("From Device"):visible',
)
_PROGRESS_SELECTORS = (
    "button:visible",
    '[role="button"]:visible',
    'input[type="button"]:visible',
    'input[type="submit"]:visible',
    'a:visible',
)
_SAFE_PROGRESS_LABELS = {
    "begin application",
    "confirm and continue",
    "continue",
    "continue application",
    "continue to application",
    "continue to next step",
    "continue to review",
    "next",
    "next step",
    "proceed",
    "review",
    "review application",
    "review your application",
    "save & continue",
    "save and continue",
    "start application",
    "upload and continue",
}
_FINAL_ACTION_WORDS = ("submit", "send application", "complete application", "finish application")
_FINAL_ACTION_LABELS = {"apply", "apply now", "complete", "finish", "send"}
_AUTH_ACTION_WORDS = (
    "continue with apple",
    "continue with email",
    "continue with google",
    "continue with microsoft",
    "log in",
    "sign in",
)


@dataclass
class ApplicationAutomationState:
    """Mutable memory for bounded progression across one employer application."""

    resume_uploaded: bool = False
    attempted_apply_urls: set[str] = field(default_factory=set)
    attempted_progress: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class ApplicationAutomationResult:
    """One observable outcome from the assisted-automation interface."""

    outcome: Literal["progressed", "needs_user", "review"]
    filled: int = 0
    missed: tuple[str, ...] = ()


def _type_human(loc, value: str) -> None:
    """Focus, clear, and type with per-char delays (looks human to anti-bot)."""
    loc.click(timeout=2000)
    try:
        loc.fill("")
    except Exception:
        pass
    loc.type(value, delay=random.randint(30, 90))

# Field types that are files (handled via the file input, not text fill).
_FILE_TYPES = {"File", "file"}
# Field types rendered as clickable option buttons (Yes/No, single choice).
_CHOICE_TYPES = {"ValueSelect", "Boolean", "MultiValueSelect"}
# Field types rendered as a typeahead combobox (e.g. Location).
_COMBOBOX_TYPES = {"Location"}


def _css_attr(value: str) -> str:
    """Escape a value for use inside a CSS attribute selector."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _by_path(page, path: str):
    """Locator for the input whose id/name equals the Ashby field path (or None)."""
    if not path:
        return None
    p = _css_attr(path)
    loc = page.locator(f'[name="{p}"], [id="{p}"]')
    try:
        if loc.count() > 0:
            return loc.first
    except Exception:
        return None
    return None


def _answer_is_visible(page, ans: dict) -> bool:
    """Return whether this planned answer belongs to the current visible step."""
    path = ans.get("name") or ""
    title = ans.get("label") or ""
    loc = _by_path(page, path)
    try:
        if loc is not None and loc.is_visible():
            return True
    except Exception:
        pass
    for target in (
        lambda: page.get_by_label(title, exact=False).first,
        lambda: page.get_by_placeholder(title, exact=False).first,
        lambda: page.get_by_text(title, exact=False).first,
    ):
        try:
            candidate = target()
            if candidate.count() > 0 and candidate.is_visible():
                return True
        except Exception:
            continue
    return False


def _fill_choice(page, title: str, value: str) -> bool:
    """Click a Yes/No / single-choice option button, scoped to its question container."""
    # Prefer a container that holds the question title, then click the option by text.
    scopes = []
    if title:
        try:
            scopes.append(page.locator(f':text("{_css_attr(title)}")').locator(
                "xpath=ancestor::*[self::div or self::fieldset][1]"))
        except Exception:
            pass
    scopes.append(page)  # fallback: whole page
    for scope in scopes:
        for target in (
            lambda: scope.get_by_role("button", name=value, exact=True),
            lambda: scope.get_by_text(value, exact=True),
        ):
            try:
                loc = target().first
                if loc.count() > 0:
                    loc.click(timeout=1500)
                    return True
            except Exception:
                continue
    return False


def _fill_combobox(page, path: str, title: str, value: str) -> bool:
    """Best-effort typeahead: type the value and pick the first suggestion."""
    box = _by_path(page, path)
    if box is None and title:
        try:
            box = page.locator(f':text("{_css_attr(title)}")').locator(
                "xpath=following::input[1]").first
        except Exception:
            box = None
    if box is None:
        return False
    try:
        box.click(timeout=1500)
        box.fill("")
        box.type(value, delay=20)
        page.wait_for_timeout(900)  # let suggestions load
        option = page.locator('[role="option"]').first
        if option.count() > 0:
            option.click(timeout=1500)
            return True
        box.press("Enter")
        return True
    except Exception:
        return False


def _fill_one(page, ans: dict) -> bool:
    """Fill one field, selecting by Ashby field path and branching on type."""
    path = ans.get("name") or ""
    value = (ans.get("answer") or "").strip()
    title = ans.get("label") or ""
    ftype = ans.get("type") or ""
    if not value:
        return False

    if ftype in _CHOICE_TYPES:
        return _fill_choice(page, title, value)
    if ftype in _COMBOBOX_TYPES:
        return _fill_combobox(page, path, title, value)

    # text-like: address the input by its path (id/name), which Ashby always sets.
    loc = _by_path(page, path)
    if loc is not None:
        try:
            if loc.input_value() == value:
                return True
            _type_human(loc, value)
            return True
        except Exception:
            try:  # a native <select> also carries the path
                loc.select_option(label=value, timeout=1500)
                return True
            except Exception:
                pass
    # Employer forms often expose equivalent variants such as legal/preferred
    # first name or email/confirm email without stable ids. Populate every
    # visible label match with the same approved fact.
    try:
        matches = page.get_by_label(title, exact=False)
        matched = False
        for index in range(min(matches.count(), 10)):
            candidate = matches.nth(index)
            if not candidate.is_visible():
                continue
            try:
                if candidate.input_value() != value:
                    _type_human(candidate, value)
                matched = True
            except Exception:
                continue
        if matched:
            return True
    except Exception:
        pass

    # Placeholder fallback is intentionally singular because broad placeholder
    # text (for example "Enter value") is not strong enough to bind siblings.
    for target in (lambda: page.get_by_placeholder(title, exact=False).first,):
        try:
            _type_human(target(), value)
            return True
        except Exception:
            continue
    return False


def _upload_resume(page, resume_path: str) -> bool:
    try:
        file_inputs = page.locator("input[type='file']")
        if file_inputs.count() > 0:
            current_name = file_inputs.first.evaluate("e => e.files?.[0]?.name || ''")
            if current_name != Path(resume_path).name:
                file_inputs.first.set_input_files(resume_path)
            return True
    except Exception:
        pass
    return False


def login_gate(page) -> bool:
    """Return whether the visible page is asking the user to authenticate."""
    application_like = False
    try:
        body_text = " ".join((page.locator("body").inner_text() or "").casefold().split())
        cues = (
            "application process",
            "personal information",
            "job specific questions",
            "review and submit",
        )
        non_password_fields = page.locator(
            'input:visible:not([type="hidden"]):not([type="search"]):not([type="password"]), '
            'select:visible, textarea:visible'
        ).count()
        application_like = non_password_fields >= 3 and sum(cue in body_text for cue in cues) >= 2
        if page.locator('input[type="password"]:visible').count() > 0 and not application_like:
            return True
    except Exception:
        pass
    for selector in _LOGIN_DIALOG_SELECTORS:
        try:
            for text in page.locator(selector).all_text_contents():
                normalized = " ".join(text.casefold().split())
                if any(word in normalized for word in _LOGIN_WORDS):
                    return True
        except Exception:
            continue
    return False


def application_form_ready(page) -> bool:
    """Detect an application form without mistaking a portal search box for one."""
    try:
        # LinkedIn Easy Apply is a multi-step modal and may expose only one
        # application field on a step. Its dedicated container is stronger
        # evidence than the generic two-input heuristic below.
        linkedin_easy_apply = page.locator(
            '.jobs-easy-apply-modal:visible input:visible, '
            '.jobs-easy-apply-modal:visible textarea:visible, '
            '[data-test-modal-id="easy-apply-modal"]:visible input:visible, '
            '[data-test-modal-id="easy-apply-modal"]:visible textarea:visible'
        )
        if linkedin_easy_apply.count() > 0:
            return True
        if page.locator('input[type="file"]:visible, textarea:visible').count() > 0:
            return True
        # Some employer portals render a multi-step application as loose
        # controls rather than a semantic <form>. Several visible required
        # fields are stronger evidence than a wrapper element.
        loose_fields = page.locator(
            'input:visible:not([type="hidden"]):not([type="search"]):not([type="password"]), '
            'select:visible, textarea:visible'
        )
        required_fields = page.locator(
            'input:visible[required]:not([type="hidden"]):not([type="search"]):not([type="password"]), '
            'input:visible[aria-required="true"]:not([type="hidden"]):not([type="search"]):not([type="password"]), '
            'select:visible[required], select:visible[aria-required="true"], '
            'textarea:visible[required], textarea:visible[aria-required="true"]'
        )
        if loose_fields.count() >= 3 and required_fields.count() >= 2:
            return True
        if loose_fields.count() >= 3:
            body_text = " ".join((page.locator("body").inner_text() or "").casefold().split())
            application_cues = (
                "application process",
                "fields marked with",
                "job specific questions",
                "personal information",
                "review and submit",
            )
            if sum(cue in body_text for cue in application_cues) >= 2:
                return True
        forms = page.locator("form:visible")
        for index in range(forms.count()):
            form = forms.nth(index)
            inputs = form.locator(
                'input:visible:not([type="hidden"]):not([type="search"]):not([type="password"])'
            )
            if inputs.count() < 2:
                continue
            text = " ".join((form.inner_text() or "").casefold().split())
            if any(word in text for word in ("resume", "cover letter", "first name", "email", "phone")):
                return True
    except Exception:
        pass
    return False


def _copy_equivalent_page_values(page) -> int:
    """Copy portal-provided facts into equivalent required fields on the step."""
    try:
        return int(page.locator("input:visible").evaluate_all(
            """elements => {
              const labelOf = (element) => (
                element.getAttribute('aria-label')
                || [...(element.labels || [])].map(label => label.textContent || '').join(' ')
                || ''
              ).replace(/\\s+/g, ' ').replace(/\\s*\\*\\s*$/, '').trim().toLowerCase();
              const fields = elements.map(element => ({element, label: labelOf(element)}));
              const legalFirst = fields.find(({element, label}) =>
                String(element.value || '').trim()
                && (label.includes('first name (legal)') || label === 'legal first name')
              );
              if (!legalFirst) return 0;
              let copied = 0;
              for (const {element, label} of fields) {
                if (!label.includes('first name (preferred)') || String(element.value || '').trim()) continue;
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                setter.call(element, legalFirst.element.value);
                element.dispatchEvent(new Event('input', {bubbles: true}));
                element.dispatchEvent(new Event('change', {bubbles: true}));
                copied += 1;
              }
              return copied;
            }"""
        ))
    except Exception:
        return 0


def _job_detail_page(page) -> bool:
    """Require multiple job-page cues before treating generic Apply as entry."""
    matches = 0
    for cue in _JOB_DETAIL_CUES:
        try:
            if page.get_by_text(cue, exact=False).count() > 0:
                matches += 1
                if matches >= 2:
                    return True
        except Exception:
            continue
    return False


def _recognized_apply_controls(page) -> list:
    """Return visible Apply entry controls that cannot be final-submit controls."""
    controls = []
    for selector in _APPLY_CONTROL_SELECTORS:
        try:
            matches = page.locator(selector)
            for index in range(min(matches.count(), 20)):
                control = matches.nth(index)
                text = " ".join((control.inner_text() or "").casefold().split())
                tracking = (control.get_attribute("data-tracking-control-name") or "").casefold()
                classes = (control.get_attribute("class") or "").casefold()
                aria_label = (control.get_attribute("aria-label") or "").casefold()
                if "apply" in text or "apply" in tracking or "apply" in classes or "apply" in aria_label:
                    controls.append(control)
        except Exception:
            continue
    if _job_detail_page(page):
        for selector in _GENERIC_APPLY_CONTROL_SELECTORS:
            try:
                matches = page.locator(selector)
                for index in range(min(matches.count(), 30)):
                    control = matches.nth(index)
                    values = (
                        control.inner_text() or "",
                        control.get_attribute("aria-label") or "",
                        control.get_attribute("value") or "",
                    )
                    label = next(
                        (" ".join(value.casefold().split()) for value in values if value.strip()),
                        "",
                    )
                    normalized = label.rstrip(" ›→»").strip()
                    if any(normalized == entry or normalized.startswith(f"{entry} ") for entry in _JOB_ENTRY_LABELS):
                        controls.append(control)
            except Exception:
                continue
    return controls


def application_control_ready(page) -> bool:
    """Return whether a recognized, non-final Apply entry control is visible."""
    return bool(_recognized_apply_controls(page))


def follow_application_control(page, attempted_urls: set[str]) -> bool:
    """Open one recognized Apply control; never clicks a form submit button."""
    try:
        current_url = page.url
    except Exception:
        return False
    if current_url in attempted_urls:
        return False

    for control in _recognized_apply_controls(page):
        try:
            href = control.get_attribute("href") or ""
            if control.get_attribute("target") == "_blank" and urlparse(href).scheme in {"http", "https"}:
                # Popup tabs from LinkedIn's external-apply safety link are
                # unreliable in a headless persistent context. Navigating the
                # managed tab reaches the identical signed destination without
                # losing the interactive session.
                page.goto(href, wait_until="commit", timeout=15_000)
            else:
                control.click(timeout=5000, no_wait_after=True)
            attempted_urls.add(current_url)
            # External Apply controls commonly create a new tab. Give
            # Playwright's context enough time to register it before the
            # session loop chooses the active page for the next step.
            page.wait_for_timeout(1000)
            return True
        except Exception:
            continue
    return False


def upload_resume_from_device(page, resume_path: str | None) -> bool:
    """Choose a recognized résumé-device option and satisfy its file chooser."""
    if not resume_path:
        return False
    for selector in _RESUME_DEVICE_SELECTORS:
        try:
            controls = page.locator(selector)
            for index in range(min(controls.count(), 10)):
                control = controls.nth(index)
                text = " ".join((control.inner_text() or "").casefold().split())
                aria_label = (control.get_attribute("aria-label") or "").casefold()
                if "from device" not in text and "from device" not in aria_label:
                    continue
                try:
                    with page.expect_file_chooser(timeout=3000) as chooser_info:
                        control.click(timeout=3000)
                    chooser_info.value.set_files(resume_path)
                    return True
                except Exception:
                    # Some portals reveal an attached/hidden input instead of
                    # emitting a native chooser event.
                    file_inputs = page.locator('input[type="file"]')
                    if file_inputs.count() > 0:
                        file_inputs.last.set_input_files(resume_path)
                        return True
        except Exception:
            continue
    return False


def _control_label(control) -> str:
    try:
        for value in (
            control.inner_text() or "",
            control.get_attribute("aria-label") or "",
            control.get_attribute("value") or "",
            control.get_attribute("title") or "",
        ):
            if value.strip():
                return " ".join(value.casefold().split())
    except Exception:
        pass
    return ""


def _visible_action_controls(page) -> list:
    controls = []
    for selector in _PROGRESS_SELECTORS:
        try:
            matches = page.locator(selector)
            controls.extend(matches.nth(index) for index in range(min(matches.count(), 40)))
        except Exception:
            continue
    return controls


def _is_safe_progress_label(label: str) -> bool:
    label = label.rstrip(" ›→»").strip()
    if not label or any(word in label for word in _FINAL_ACTION_WORDS + _AUTH_ACTION_WORDS):
        return False
    return label in _SAFE_PROGRESS_LABELS or label.startswith(("continue to ", "save and continue"))


def _page_signature(page, label: str) -> str:
    try:
        body = " ".join((page.locator("body").inner_text(timeout=1000) or "").split())[:6000]
        controls = page.locator("input:visible, select:visible, textarea:visible, button:visible").evaluate_all(
            "elements => elements.map(element => [element.tagName, element.name, element.id, "
            "element.getAttribute('aria-label'), element.type])"
        )
        material = f"{page.url}\n{label}\n{body}\n{controls}"
    except Exception:
        material = f"{getattr(page, 'url', '')}\n{label}"
    return sha256(material.encode("utf-8", errors="ignore")).hexdigest()


def application_state_signature(page) -> str:
    """Hash application-relevant structure without retaining entered values."""
    try:
        controls = page.locator(
            "input:visible, select:visible, textarea:visible, button:visible, a:visible"
        ).evaluate_all(
            """elements => elements.map(element => ({
              tag: element.tagName,
              type: element.type || '',
              name: element.name || '',
              id: element.id || '',
              label: element.getAttribute('aria-label') || '',
              text: ['BUTTON', 'A'].includes(element.tagName)
                ? String(element.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 160)
                : '',
              populated: ['checkbox', 'radio'].includes(element.type)
                ? Boolean(element.checked)
                : Boolean(String(element.value || '').trim()),
            }))"""
        )
        material = f"{page.url}\n{controls}"
    except Exception:
        material = str(getattr(page, "url", ""))
    return sha256(material.encode("utf-8", errors="ignore")).hexdigest()


def _follow_safe_progress(page, state: ApplicationAutomationState) -> bool:
    """Click one allow-listed reversible step control, never a final action."""
    for control in _visible_action_controls(page):
        label = _control_label(control)
        if not _is_safe_progress_label(label):
            continue
        try:
            if control.is_disabled() or control.get_attribute("aria-disabled") == "true":
                continue
        except Exception:
            continue
        fingerprint = _page_signature(page, label)
        if fingerprint in state.attempted_progress:
            continue
        state.attempted_progress.add(fingerprint)
        try:
            control.click(timeout=3000)
            page.wait_for_timeout(350)
            return True
        except Exception:
            continue
    return False


def _final_action_ready(page) -> bool:
    for control in _visible_action_controls(page):
        label = _control_label(control).rstrip(" ›→»").strip()
        if label in _FINAL_ACTION_LABELS or any(word in label for word in _FINAL_ACTION_WORDS):
            return True
    return False


def _critical_gate(page) -> str | None:
    selectors = (
        'iframe[src*="captcha" i]:visible',
        '.g-recaptcha:visible',
        '[data-sitekey]:visible',
        'input[autocomplete="one-time-code"]:visible',
        'input[name*="otp" i]:visible',
        'input[name*="verification" i]:visible',
    )
    for selector in selectors:
        try:
            if page.locator(selector).count() > 0:
                return "Complete CAPTCHA or account verification"
        except Exception:
            continue
    return None


def _unresolved_required_fields(page) -> tuple[str, ...]:
    try:
        labels = page.locator("input:visible, select:visible, textarea:visible").evaluate_all(
            """elements => elements.flatMap((element) => {
              if (element.disabled || ['hidden', 'search', 'file', 'button', 'submit', 'reset'].includes(element.type)) return [];
              const labelText = element.getAttribute('aria-label')
                || [...(element.labels || [])].map((item) => {
                  const copy = item.cloneNode(true);
                  copy.querySelectorAll('input, select, textarea, button').forEach(control => control.remove());
                  return copy.textContent;
                }).join(' ')
                || element.placeholder || element.name || 'Required field';
              const required = element.required
                || element.getAttribute('aria-required') === 'true'
                || /\*\s*$/.test(labelText.trim());
              const hasValue = element.type === 'checkbox' || element.type === 'radio'
                ? element.checked
                : String(element.value || '').trim().length > 0;
              const invalidValue = hasValue && element.willValidate && !element.checkValidity();
              if ((!required || hasValue) && !invalidValue) return [];
              if (element.type === 'radio' && element.name) {
                const escaped = CSS.escape(element.name);
                if (document.querySelector(`input[type="radio"][name="${escaped}"]:checked`)) return [];
              }
              return [labelText.replace(/\s+/g, ' ').replace(/\s*\*\s*$/, '').trim()];
            })"""
        )
        return tuple(dict.fromkeys(label for label in labels if label))
    except Exception:
        return ()


def advance_application_step(
    page,
    answers: list[dict],
    resume_path: str | None,
    state: ApplicationAutomationState,
    on_update: Callable[[object], None] | None = None,
) -> ApplicationAutomationResult:
    """Perform one safe browser step or return the critical reason for handoff.

    The interface deliberately excludes final-submit authority. Callers can loop
    on ``progressed`` and stop on ``needs_user`` or ``review``.
    """
    critical = _critical_gate(page)
    if critical:
        return ApplicationAutomationResult("needs_user", missed=(critical,))

    if not state.resume_uploaded and upload_resume_from_device(page, resume_path):
        state.resume_uploaded = True
        if on_update:
            on_update(page)
        return ApplicationAutomationResult("progressed", filled=1)

    form_ready = application_form_ready(page) or any(
        _answer_is_visible(page, answer) for answer in answers
    )
    if not form_ready and follow_application_control(page, state.attempted_apply_urls):
        if on_update:
            on_update(page)
        return ApplicationAutomationResult("progressed")

    filled = 0
    missed: list[str] = []
    if form_ready:
        filled, missed = fill_form(
            page,
            answers,
            None if state.resume_uploaded else resume_path,
            on_update,
        )
        filled += _copy_equivalent_page_values(page)
        try:
            if resume_path and page.locator('input[type="file"]').evaluate_all(
                "elements => elements.some(element => element.files?.length)"
            ):
                state.resume_uploaded = True
        except Exception:
            pass
        unresolved = _unresolved_required_fields(page)
        if unresolved:
            return ApplicationAutomationResult("needs_user", filled=filled, missed=unresolved)

    if _final_action_ready(page):
        return ApplicationAutomationResult("review", filled=filled, missed=("Review and submit",))

    if _follow_safe_progress(page, state):
        if on_update:
            on_update(page)
        return ApplicationAutomationResult("progressed", filled=filled)

    if missed:
        return ApplicationAutomationResult(
            "needs_user", filled=filled, missed=tuple(dict.fromkeys(missed))
        )
    return ApplicationAutomationResult(
        "needs_user",
        filled=filled,
        missed=("Choose the next application step",),
    )


def fill_form(
    page,
    answers: list[dict],
    resume_path: str | None,
    on_update: Callable[[object], None] | None = None,
) -> tuple[int, list[str]]:
    """Fill all planned answers on the current page. Returns (filled, missed labels).

    The résumé is uploaded FIRST so Ashby's own 'autofill from resume' re-render
    settles before we write our authoritative values.
    """
    filled = 0
    missed: list[str] = []

    if resume_path:
        try:
            has_upload = page.locator("input[type='file']").count() > 0
        except Exception:
            has_upload = False
        if has_upload and _upload_resume(page, resume_path):
            filled += 1
            page.wait_for_timeout(2500)  # let Ashby's resume-autofill re-render settle
        elif has_upload:
            missed.append("Résumé")

        if on_update:
            on_update(page)

    for a in answers:
        if a.get("type") in _FILE_TYPES:
            continue
        if not (a.get("answer") or "").strip():
            continue
        if not _answer_is_visible(page, a):
            continue
        if _fill_one(page, a):
            filled += 1
        else:
            missed.append(a.get("label") or a.get("name") or "field")
        if on_update:
            on_update(page)

    return filled, missed


def open_and_fill(
    context,
    apply_url: str,
    answers: list[dict],
    resume_path: str | None,
    on_update: Callable[[object], None] | None = None,
):
    """Open a new tab in an existing (persistent) context and fill it.

    Returns (page, filled, missed). Does NOT launch a browser, wait, or submit —
    that is the managed session's job. Used by the assist worker and by tests.
    """
    page = context.new_page()
    page.goto(apply_url, wait_until="load")
    if on_update:
        on_update(page)
    try:  # wait for the React form to hydrate before filling
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    if login_gate(page):
        if on_update:
            on_update(page)
        log.info("authentication handoff required before assisted fill (%s)", apply_url)
        return page, 0, ["Sign in required"]
    if application_control_ready(page) and not application_form_ready(page):
        if on_update:
            on_update(page)
        return page, 0, ["Open the employer application"]
    try:
        page.wait_for_selector("input[name], input[id], textarea, input[type='file']", timeout=8000)
    except Exception:
        pass
    if login_gate(page):
        if on_update:
            on_update(page)
        log.info("authentication handoff required before assisted fill (%s)", apply_url)
        return page, 0, ["Sign in required"]
    filled, missed = fill_form(page, answers, resume_path, on_update)
    log.info("assisted fill: %d filled, missed=%s (%s)", filled, missed, apply_url)
    return page, filled, missed
