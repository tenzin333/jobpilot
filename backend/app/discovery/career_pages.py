"""Generic career-page connector (opt-in, brittle).

Scrapes job links from a configured page with Playwright. These jobs cannot be
auto-submitted (no standard ATS form), so submission routes them to the
intervention queue (handled upstream: career_page has no submit adapter).
"""
from __future__ import annotations

from urllib.parse import urljoin, urlparse

from app.config import CareerSite
from app.discovery.base import RawJob
from app.models import AtsType


def _company_from_url(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc.split(":")[0] or url


def parse_links(site_url: str, anchors: list[tuple[str, str]], keyword: str = "") -> list[RawJob]:
    """Pure parser: (text, href) anchors -> RawJob list. Dedupes by absolute URL."""
    company = _company_from_url(site_url)
    results: list[RawJob] = []
    seen: set[str] = set()
    for text, href in anchors:
        if not href:
            continue
        url = urljoin(site_url, href)
        title = (text or "").strip()
        if not title or url in seen:
            continue
        if keyword and keyword.lower() not in f"{title.lower()} {url.lower()}":
            continue
        seen.add(url)
        results.append(
            RawJob(
                source=AtsType.career_page.value,
                source_job_id=url,
                company=company,
                title=title,
                apply_url=url,
                raw={"site": site_url},
            )
        )
    return results


class CareerPagesConnector:
    name = AtsType.career_page.value

    async def fetch_site(self, site: CareerSite) -> list[RawJob]:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            try:
                await page.goto(site.url, wait_until="load")
                anchors = await page.eval_on_selector_all(
                    site.link_selector or "a",
                    "els => els.map(e => [e.textContent, e.getAttribute('href')])",
                )
            finally:
                await browser.close()
        return parse_links(site.url, anchors, site.keyword)
