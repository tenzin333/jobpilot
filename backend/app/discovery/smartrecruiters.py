"""SmartRecruiters public postings connector (no key).

API: https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100
Returns {"content": [...]}. The list endpoint has no full description; the apply
URL is reconstructed to the public posting page.
"""
from __future__ import annotations

import httpx

from app.discovery.base import RawJob
from app.models import AtsType

BASE = "https://api.smartrecruiters.com/v1/companies"


def _location_str(loc: dict) -> str:
    parts = [loc.get("city"), loc.get("region"), loc.get("country")]
    return ", ".join(p for p in parts if p)


def parse_jobs(company_slug: str, content: list[dict]) -> list[RawJob]:
    results: list[RawJob] = []
    for post in content:
        loc = post.get("location") or {}
        company = (post.get("company") or {}).get("name") or company_slug
        job_id = str(post.get("id", ""))
        results.append(
            RawJob(
                source=AtsType.smartrecruiters.value,
                source_job_id=job_id,
                company=company,
                title=post.get("name", "") or "",
                location=_location_str(loc),
                remote=bool(loc.get("remote")),
                description="",  # full text requires a per-posting detail call
                apply_url=f"https://jobs.smartrecruiters.com/{company_slug}/{job_id}",
                raw=post,
            )
        )
    return results


class SmartRecruitersConnector:
    name = AtsType.smartrecruiters.value

    async def fetch(self, company: str) -> list[RawJob]:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{BASE}/{company}/postings", params={"limit": 100})
            resp.raise_for_status()
            data = resp.json()
        return parse_jobs(company, data.get("content", []))
