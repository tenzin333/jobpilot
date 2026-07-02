"""Greenhouse public job-board connector.

Board jobs:  https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true
Board meta:  https://boards-api.greenhouse.io/v1/boards/{company}
No auth required.
"""
from __future__ import annotations

import html
import re

import httpx

from app.discovery.base import RawJob
from app.models import AtsType

_TAG_RE = re.compile(r"<[^>]+>")
BASE = "https://boards-api.greenhouse.io/v1/boards"


def _strip_html(content: str) -> str:
    return _TAG_RE.sub(" ", html.unescape(content or "")).strip()


def _is_remote(location: str, title: str) -> bool:
    blob = f"{location} {title}".lower()
    return "remote" in blob


def parse_jobs(company_name: str, jobs: list[dict]) -> list[RawJob]:
    """Pure parser: Greenhouse jobs JSON -> RawJob list (no network)."""
    results: list[RawJob] = []
    for job in jobs:
        location = (job.get("location") or {}).get("name", "") or ""
        title = job.get("title", "") or ""
        results.append(
            RawJob(
                source=AtsType.greenhouse.value,
                source_job_id=str(job.get("id", "")),
                company=company_name,
                title=title,
                location=location,
                remote=_is_remote(location, title),
                description=_strip_html(job.get("content", "")),
                apply_url=job.get("absolute_url", "") or "",
                raw=job,
            )
        )
    return results


class GreenhouseConnector:
    name = AtsType.greenhouse.value

    async def fetch(self, company: str) -> list[RawJob]:
        async with httpx.AsyncClient(timeout=30) as client:
            # Board display name (fallback to the slug if unavailable).
            company_name = company
            try:
                meta = await client.get(f"{BASE}/{company}")
                if meta.status_code == 200:
                    company_name = meta.json().get("name") or company
            except httpx.HTTPError:
                pass

            resp = await client.get(f"{BASE}/{company}/jobs", params={"content": "true"})
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])

        return parse_jobs(company_name, jobs)
