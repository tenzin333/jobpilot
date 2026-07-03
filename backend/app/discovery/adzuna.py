"""Adzuna job search API (optional; free app id/key from developer.adzuna.com).

API: https://api.adzuna.com/v1/api/jobs/{country}/search/{page}
     ?app_id=..&app_key=..&what=<keyword>&where=<location>&results_per_page=50
Broad aggregator with salary data. Apply links are external -> needs_human.
"""
from __future__ import annotations

import httpx

from app.config import Preferences, SourceConfig, get_settings
from app.discovery.base import RawJob
from app.discovery.util import is_remote, strip_html
from app.models import AtsType

BASE = "https://api.adzuna.com/v1/api/jobs"


def parse_jobs(results: list[dict]) -> list[RawJob]:
    """Pure parser: Adzuna results -> RawJob list."""
    out: list[RawJob] = []
    for job in results:
        title = job.get("title", "") or ""
        location = (job.get("location") or {}).get("display_name", "") or ""
        smin = job.get("salary_min")
        smax = job.get("salary_max")
        out.append(
            RawJob(
                source=AtsType.adzuna.value,
                source_job_id=str(job.get("id", "")),
                company=(job.get("company") or {}).get("display_name", "") or "",
                title=title,
                location=location,
                remote=is_remote(location, title),
                salary_min=int(smin) if smin else None,
                salary_max=int(smax) if smax else None,
                description=strip_html(job.get("description", "")),
                apply_url=job.get("redirect_url", "") or "",
                raw=job,
            )
        )
    return out


class AdzunaConnector:
    name = AtsType.adzuna.value

    async def fetch_jobs(self, prefs: Preferences, cfg: SourceConfig) -> list[RawJob]:
        settings = get_settings()
        if not (settings.adzuna_app_id and settings.adzuna_app_key):
            return []  # not configured -> silently skip

        queries = prefs.desired_roles or [""]
        where = prefs.locations[0] if prefs.locations else ""
        country = settings.adzuna_country or "us"
        out: list[RawJob] = []
        async with httpx.AsyncClient(timeout=30) as client:
            for query in queries:
                for page in range(1, max(cfg.max_pages, 1) + 1):
                    params = {
                        "app_id": settings.adzuna_app_id,
                        "app_key": settings.adzuna_app_key,
                        "what": query,
                        "results_per_page": 50,
                    }
                    if where and where.lower() != "remote":
                        params["where"] = where
                    try:
                        resp = await client.get(f"{BASE}/{country}/search/{page}", params=params)
                        resp.raise_for_status()
                        out.extend(parse_jobs(resp.json().get("results", [])))
                    except httpx.HTTPError:
                        break
        return out
