"""Lever-hosted application form adapter."""
from __future__ import annotations

from app.models import AtsType, Profile
from app.submit.base import FieldMap, SubmitResult, run_form_submit

_FIELD_MAP = FieldMap(
    name=["input[name='name']", "input[autocomplete='name']", "input[name*='name']"],
    email=["input[name='email']", "input[type='email']", "input[name*='email']"],
    resume_file=["input[type='file'][name='resume']", "input[type='file']"],
    submit=["button[type='submit']", "input[type='submit']", "button:has-text('Submit application')"],
)


class LeverSubmitAdapter:
    name = AtsType.lever.value
    field_map = _FIELD_MAP

    def submit(self, *, apply_url: str, profile: Profile, resume_path: str, dry_run: bool) -> SubmitResult:
        return run_form_submit(
            apply_url=apply_url, profile=profile, resume_path=resume_path,
            dry_run=dry_run, field_map=self.field_map,
        )
