"""The Muse public job API (no key).

API: https://www.themuse.com/api/public/jobs?category=...&location=...&page=N
The Muse has no free-text query param, so we fetch by category and filter by the
user's desired-role keywords client-side. Apply links are external -> needs_human.
"""
from __future__ import annotations

import httpx

from app.config import Preferences, SourceConfig
from app.discovery.base import RawJob
from app.discovery.util import is_remote, keyword_in_title, strip_html
from app.models import AtsType

API = "https://www.themuse.com/api/public/jobs"
# Valid Muse category names (verified against the API).
_DEFAULT_CATEGORIES = ["Software Engineering", "Computer and IT", "Data and Analytics"]


def parse_jobs(jobs: list[dict], keywords: list[str]) -> list[RawJob]:
    """Pure parser: Muse job results -> RawJob list, filtered by keywords."""
    results: list[RawJob] = []
    for job in jobs:
        title = job.get("name", "") or ""
        company = (job.get("company") or {}).get("name", "") or ""
        locations = ", ".join(loc.get("name", "") for loc in (job.get("locations") or []))
        description = strip_html(job.get("contents", ""))
        if not keyword_in_title(title, keywords):
            continue
        results.append(
            RawJob(
                source=AtsType.themuse.value,
                source_job_id=str(job.get("id", "")),
                company=company,
                title=title,
                location=locations,
                remote=is_remote(locations, title),
                description=description,
                apply_url=(job.get("refs") or {}).get("landing_page", "") or "",
                raw=job,
            )
        )
    return results


class TheMuseConnector:
    name = AtsType.themuse.value

    async def fetch_jobs(self, prefs: Preferences, cfg: SourceConfig) -> list[RawJob]:
        categories = cfg.categories or _DEFAULT_CATEGORIES
        keywords = prefs.desired_roles
        out: list[RawJob] = []
        async with httpx.AsyncClient(timeout=30) as client:
            # One request per category (multi-category in a single call is unreliable).
            for cat in categories:
                for page in range(1, max(cfg.max_pages, 1) + 1):
                    try:
                        resp = await client.get(API, params={"page": page, "category": cat})
                        resp.raise_for_status()
                        out.extend(parse_jobs(resp.json().get("results", []), keywords))
                    except httpx.HTTPError:
                        break
        return out
