"""Remotive remote-jobs API (no key).

API: https://remotive.com/api/remote-jobs?search=...&limit=N
Supports free-text `search`, so we query each desired role. Remote-only board.
Apply links are external -> needs_human.
"""
from __future__ import annotations

import asyncio

import httpx

from app.config import Preferences, SourceConfig
from app.discovery.base import RawJob
from app.discovery.util import keyword_in_title, strip_html
from app.models import AtsType

API = "https://remotive.com/api/remote-jobs"


def _parse_salary(text: str) -> tuple[int | None, int | None]:
    """Best-effort parse of Remotive's free-form salary string (e.g. '$120k - $150k')."""
    import re

    nums = re.findall(r"(\d[\d,]*)\s*k?", (text or "").lower())
    vals: list[int] = []
    for raw in nums:
        n = int(raw.replace(",", ""))
        if "k" in (text or "").lower() and n < 1000:
            n *= 1000
        vals.append(n)
    vals = [v for v in vals if v >= 1000]
    if not vals:
        return None, None
    return min(vals), max(vals)


def parse_jobs(jobs: list[dict]) -> list[RawJob]:
    """Pure parser: Remotive jobs -> RawJob list."""
    results: list[RawJob] = []
    for job in jobs:
        smin, smax = _parse_salary(job.get("salary", ""))
        results.append(
            RawJob(
                source=AtsType.remotive.value,
                source_job_id=str(job.get("id", "")),
                company=job.get("company_name", "") or "",
                title=job.get("title", "") or "",
                location=job.get("candidate_required_location", "") or "Remote",
                remote=True,
                salary_min=smin,
                salary_max=smax,
                description=strip_html(job.get("description", "")),
                apply_url=job.get("url", "") or "",
                raw=job,
            )
        )
    return results


class RemotiveConnector:
    name = AtsType.remotive.value

    async def fetch_jobs(self, prefs: Preferences, cfg: SourceConfig) -> list[RawJob]:
        queries = prefs.desired_roles or [""]
        keywords = prefs.desired_roles  # post-filter: Remotive's search is broad
        out: list[RawJob] = []

        async with httpx.AsyncClient(timeout=20) as client:
            async def _query(q: str) -> list[RawJob]:
                try:
                    resp = await client.get(API, params={"search": q, "limit": 50})
                    resp.raise_for_status()
                    return parse_jobs(resp.json().get("jobs", []))
                except httpx.HTTPError:
                    return []

            results = await asyncio.gather(*(_query(q) for q in queries))

        for batch in results:
            for job in batch:
                if keyword_in_title(job.title, keywords):
                    out.append(job)
        return out
