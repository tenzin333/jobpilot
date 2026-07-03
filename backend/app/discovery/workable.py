"""Workable public account jobs connector (no key).

API: https://www.workable.com/api/accounts/{company}?details=true
Returns {"name": ..., "jobs": [...]} with HTML descriptions.
"""
from __future__ import annotations

import httpx

from app.discovery.base import RawJob
from app.discovery.util import is_remote, strip_html
from app.models import AtsType

BASE = "https://www.workable.com/api/accounts"


def _location_str(loc: dict) -> str:
    parts = [loc.get("city"), loc.get("region"), loc.get("country")]
    return ", ".join(p for p in parts if p)


def parse_jobs(company_name: str, jobs: list[dict]) -> list[RawJob]:
    results: list[RawJob] = []
    for job in jobs:
        loc = job.get("location") or {}
        title = job.get("title", "") or ""
        location = _location_str(loc)
        results.append(
            RawJob(
                source=AtsType.workable.value,
                source_job_id=str(job.get("shortcode") or job.get("id", "")),
                company=company_name,
                title=title,
                location=location,
                remote=bool(loc.get("telecommuting")) or is_remote(location, title),
                description=strip_html(job.get("description", "")),
                apply_url=job.get("application_url") or job.get("url") or job.get("shortlink", "") or "",
                raw=job,
            )
        )
    return results


class WorkableConnector:
    name = AtsType.workable.value

    async def fetch(self, company: str) -> list[RawJob]:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(f"{BASE}/{company}", params={"details": "true"})
            resp.raise_for_status()
            data = resp.json()
        company_name = data.get("name") or company
        return parse_jobs(company_name, data.get("jobs", []))
