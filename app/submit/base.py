"""Submission result types, the adapter protocol, and a generic form runner.

Safety model:
- Always run `classify_form` BEFORE filling; bail to NeedsHuman on essays/video/captcha.
- In DRY_RUN, fill fields but NEVER click the final submit (returns DryRunFilled).
- Per-source autonomy + the global kill switch are enforced by the orchestrator,
  not here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.models import Profile
from app.submit.detect import classify_form


@dataclass
class Submitted:
    pass


@dataclass
class DryRunFilled:
    pass


@dataclass
class NeedsHuman:
    reason: str


@dataclass
class Failed:
    error: str


SubmitResult = Submitted | DryRunFilled | NeedsHuman | Failed


@dataclass
class FieldMap:
    """CSS selectors for an ATS's standard fields (best-effort; missing is OK)."""

    name: list[str]
    email: list[str]
    resume_file: list[str]
    submit: list[str]


class SubmitAdapter(Protocol):
    name: str
    field_map: FieldMap

    def submit(self, *, apply_url: str, profile: Profile, resume_path: str, dry_run: bool) -> SubmitResult: ...


def _fill_first(page, selectors: list[str], value: str) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.fill(value)
                return True
        except Exception:
            continue
    return False


def _set_file_first(page, selectors: list[str], path: str) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.set_input_files(path)
                return True
        except Exception:
            continue
    return False


def run_form_submit(
    *, apply_url: str, profile: Profile, resume_path: str, dry_run: bool, field_map: FieldMap
) -> SubmitResult:
    """Generic navigate -> detect -> fill -> (submit) flow used by ATS adapters."""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            try:
                page.goto(apply_url, wait_until="load")
                reason = classify_form(page.content())
                if reason:
                    return NeedsHuman(reason)

                if profile.full_name:
                    _fill_first(page, field_map.name, profile.full_name)
                if profile.email:
                    _fill_first(page, field_map.email, profile.email)
                if resume_path:
                    _set_file_first(page, field_map.resume_file, resume_path)

                if dry_run:
                    return DryRunFilled()

                for sel in field_map.submit:
                    loc = page.locator(sel)
                    if loc.count() > 0:
                        loc.first.click()
                        return Submitted()
                return Failed("submit button not found")
            finally:
                browser.close()
    except Exception as exc:  # navigation/automation failure
        return Failed(f"{type(exc).__name__}: {exc}")
