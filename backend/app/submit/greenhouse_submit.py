"""Greenhouse-hosted application form adapter."""
from __future__ import annotations

from app.models import AtsType, Profile
from app.submit.base import FieldMap, SubmitResult, run_form_submit

_FIELD_MAP = FieldMap(
    name=["#first_name", "input[name='job_application[first_name]']", "input[name*='name']", "input[autocomplete='name']"],
    email=["#email", "input[type='email']", "input[name*='email']"],
    resume_file=["input[type='file'][name*='resume']", "input[type='file']"],
    submit=["#submit_app", "button[type='submit']", "input[type='submit']", "button:has-text('Submit')"],
)


class GreenhouseSubmitAdapter:
    name = AtsType.greenhouse.value
    field_map = _FIELD_MAP

    def submit(self, *, apply_url: str, profile: Profile, resume_path: str, dry_run: bool) -> SubmitResult:
        return run_form_submit(
            apply_url=apply_url, profile=profile, resume_path=resume_path,
            dry_run=dry_run, field_map=self.field_map,
        )
