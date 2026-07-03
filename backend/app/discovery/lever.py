"""Lever public postings connector.

Postings:  https://api.lever.co/v0/postings/{company}?mode=json
No auth required. Returns a JSON list of postings.
"""
from __future__ import annotations

import httpx

from app.discovery.base import RawJob
from app.models import AtsType

BASE = "https://api.lever.co/v0/postings"


def parse_postings(company: str, postings: list[dict]) -> list[RawJob]:
    """Pure parser: Lever postings JSON -> RawJob list (no network)."""
    results: list[RawJob] = []
    for post in postings:
        categories = post.get("categories") or {}
        location = categories.get("location", "") or ""
        workplace = (post.get("workplaceType") or "").lower()
        results.append(
            RawJob(
                source=AtsType.lever.value,
                source_job_id=str(post.get("id", "")),
                company=company,
                title=post.get("text", "") or "",
                location=location,
                remote=workplace == "remote" or "remote" in location.lower(),
                description=post.get("descriptionPlain", "") or "",
                apply_url=post.get("applyUrl") or post.get("hostedUrl", "") or "",
                raw=post,
            )
        )
    return results


class LeverConnector:
    name = AtsType.lever.value

    async def fetch(self, company: str) -> list[RawJob]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{BASE}/{company}", params={"mode": "json"})
            resp.raise_for_status()
            postings = resp.json()
        return parse_postings(company, postings)
