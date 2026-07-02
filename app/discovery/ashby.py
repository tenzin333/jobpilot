"""Ashby public job-board connector (no key).

API: https://api.ashbyhq.com/posting-api/job-board/{company}?includeCompensation=true
Returns {"jobs": [...]}. Popular with AI startups.
"""
from __future__ import annotations

import httpx

from app.discovery.base import RawJob
from app.discovery.util import is_remote, strip_html
from app.models import AtsType

BASE = "https://api.ashbyhq.com/posting-api/job-board"


def parse_jobs(company_name: str, jobs: list[dict]) -> list[RawJob]:
    results: list[RawJob] = []
    for job in jobs:
        title = job.get("title", "") or ""
        location = job.get("location", "") or ""
        description = job.get("descriptionPlain") or strip_html(job.get("descriptionHtml", ""))
        results.append(
            RawJob(
                source=AtsType.ashby.value,
                source_job_id=str(job.get("id", "")),
                company=company_name,
                title=title,
                location=location,
                remote=bool(job.get("isRemote")) or is_remote(location, title),
                description=description,
                apply_url=job.get("applyUrl") or job.get("jobUrl", "") or "",
                raw=job,
            )
        )
    return results


class AshbyConnector:
    name = AtsType.ashby.value

    async def fetch(self, company: str) -> list[RawJob]:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{BASE}/{company}", params={"includeCompensation": "true"})
            resp.raise_for_status()
            data = resp.json()
        return parse_jobs(company, data.get("jobs", []))
