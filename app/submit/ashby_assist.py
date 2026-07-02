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

log = logging.getLogger("assist")

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
            loc.fill(value, timeout=2000)
            return True
        except Exception:
            try:  # a native <select> also carries the path
                loc.select_option(label=value, timeout=1500)
                return True
            except Exception:
                pass
    # fallbacks for forms that don't expose a path (label / placeholder).
    for target in (
        lambda: page.get_by_label(title, exact=False).first,
        lambda: page.get_by_placeholder(title, exact=False).first,
    ):
        try:
            target().fill(value, timeout=1500)
            return True
        except Exception:
            continue
    return False


def _upload_resume(page, resume_path: str) -> bool:
    try:
        file_inputs = page.locator("input[type='file']")
        if file_inputs.count() > 0:
            file_inputs.first.set_input_files(resume_path)
            return True
    except Exception:
        pass
    return False


def fill_form(page, answers: list[dict], resume_path: str | None) -> tuple[int, list[str]]:
    """Fill all planned answers on the current page. Returns (filled, missed labels).

    The résumé is uploaded FIRST so Ashby's own 'autofill from resume' re-render
    settles before we write our authoritative values.
    """
    filled = 0
    missed: list[str] = []

    if resume_path:
        if _upload_resume(page, resume_path):
            filled += 1
            page.wait_for_timeout(2500)  # let Ashby's resume-autofill re-render settle
        else:
            missed.append("Résumé")

    for a in answers:
        if a.get("type") in _FILE_TYPES:
            continue
        if not (a.get("answer") or "").strip():
            continue
        if _fill_one(page, a):
            filled += 1
        else:
            missed.append(a.get("label") or a.get("name") or "field")

    return filled, missed


def open_and_fill(context, apply_url: str, answers: list[dict], resume_path: str | None):
    """Open a new tab in an existing (persistent) context and fill it.

    Returns (page, filled, missed). Does NOT launch a browser, wait, or submit —
    that is the managed session's job. Used by the assist worker and by tests.
    """
    page = context.new_page()
    page.goto(apply_url, wait_until="load")
    try:  # wait for the React form to hydrate before filling
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    try:
        page.wait_for_selector("input[name], input[id], textarea, input[type='file']", timeout=8000)
    except Exception:
        pass
    filled, missed = fill_form(page, answers, resume_path)
    log.info("assisted fill: %d filled, missed=%s (%s)", filled, missed, apply_url)
    return page, filled, missed
