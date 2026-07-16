"""LinkedIn public "guest" job search (no key, no login).

Endpoint: https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
          ?keywords=<role>&location=<where>&start=<offset>
Returns an HTML fragment of ~10 `<li>` job cards per call (no JSON, no auth).
We query each desired role and paginate via `start`. Apply links are external
LinkedIn posting URLs -> needs_human. Heavily rate-limited: a 429/HTTP error
just ends that query's pagination (best-effort, like the other aggregators).
"""
from __future__ import annotations

import asyncio
import re

import httpx

from app.config import Preferences, SourceConfig
from app.discovery.base import RawJob
from app.discovery.util import is_remote, strip_html
from app.models import AtsType

API = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)
# LinkedIn 400s guest requests without a browser-like UA.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}
_PER_PAGE = 10  # cards the guest endpoint returns per request

_CARD_RE = re.compile(r"<li\b.*?</li>", re.DOTALL)
_TITLE_RE = re.compile(r'base-search-card__title[^>]*>(.*?)</h3>', re.DOTALL)
_COMPANY_RE = re.compile(r'base-search-card__subtitle[^>]*>(.*?)</h4>', re.DOTALL)
_LOCATION_RE = re.compile(r'job-search-card__location[^>]*>(.*?)</span>', re.DOTALL)
# The anchor's class attribute carries extra utility classes after the marker
# (e.g. `base-card__full-link absolute top-0 …`), so match up to the closing
# quote of the class attr rather than requiring the marker to end it.
_LINK_RE = re.compile(r'base-card__full-link[^"]*"[^>]*href="([^"?]+)')
_URN_RE = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"')


def _job_id(card: str, url: str) -> str:
    m = _URN_RE.search(card)
    if m:
        return m.group(1)
    tail = re.search(r"-(\d+)(?:/)?$", url)  # .../jobs/view/some-slug-3812345678
    return tail.group(1) if tail else url


def parse_jobs(html_fragment: str) -> list[RawJob]:
    """Pure parser: a LinkedIn guest-search HTML fragment -> RawJob list."""
    results: list[RawJob] = []
    for card in _CARD_RE.findall(html_fragment or ""):
        link = _LINK_RE.search(card)
        title = _TITLE_RE.search(card)
        if not link or not title:
            continue
        url = link.group(1).strip()
        title_text = strip_html(title.group(1))
        company = _COMPANY_RE.search(card)
        location = _LOCATION_RE.search(card)
        company_text = strip_html(company.group(1)) if company else ""
        location_text = strip_html(location.group(1)) if location else ""
        results.append(
            RawJob(
                source=AtsType.linkedin.value,
                source_job_id=_job_id(card, url),
                company=company_text,
                title=title_text,
                location=location_text,
                remote=is_remote(location_text, title_text),
                description="",  # guest cards carry no description
                apply_url=url,
                raw={"html_card": card},
            )
        )
    return results


class LinkedInConnector:
    name = AtsType.linkedin.value

    async def fetch_jobs(self, prefs: Preferences, cfg: SourceConfig) -> list[RawJob]:
        queries = prefs.desired_roles or [""]
        where = prefs.locations[0] if prefs.locations else ""
        pages = max(cfg.max_pages, 1)

        async with httpx.AsyncClient(timeout=30, headers=_HEADERS) as client:
            async def _query(role: str) -> list[RawJob]:
                jobs: list[RawJob] = []
                for page in range(pages):
                    params = {"keywords": role, "start": page * _PER_PAGE}
                    if where and where.lower() != "remote":
                        params["location"] = where
                    try:
                        resp = await client.get(API, params=params)
                        resp.raise_for_status()
                    except httpx.HTTPError:
                        break  # rate-limited / transient -> stop paging this role
                    batch = parse_jobs(resp.text)
                    if not batch:
                        break  # no more results
                    jobs.extend(batch)
                return jobs

            results = await asyncio.gather(*(_query(q) for q in queries))

        # De-dup within this fetch by posting id (same role pages can overlap).
        seen: set[str] = set()
        out: list[RawJob] = []
        for batch in results:
            for job in batch:
                if job.source_job_id in seen:
                    continue
                seen.add(job.source_job_id)
                out.append(job)
        return out
