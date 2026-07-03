"""Phase 8 acceptance: generic career-page parsing + real Playwright scrape."""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest  # noqa: E402

from app.config import CareerSite  # noqa: E402
from app.discovery.career_pages import CareerPagesConnector, parse_links  # noqa: E402


def test_parse_links_basic():
    anchors = [
        ("Senior Engineer", "/jobs/1"),
        ("Senior Engineer", "/jobs/1"),  # duplicate URL -> deduped
        ("Marketing Lead", "https://other.com/jobs/2"),
        ("", "/jobs/3"),                 # empty title -> skipped
    ]
    raws = parse_links("https://acme.com/careers", anchors)
    urls = {r.apply_url for r in raws}
    assert "https://acme.com/jobs/1" in urls          # relative -> absolute
    assert "https://other.com/jobs/2" in urls
    assert len(raws) == 2                              # dup + empty removed
    assert all(r.company == "acme.com" for r in raws)
    assert all(r.source == "career_page" for r in raws)


def test_parse_links_keyword_filter():
    anchors = [("Backend Engineer", "/e"), ("Office Manager", "/m")]
    raws = parse_links("https://acme.com", anchors, keyword="engineer")
    assert len(raws) == 1
    assert raws[0].title == "Backend Engineer"


@pytest.mark.asyncio
async def test_fetch_site_real(tmp_path: Path):
    html = """<!doctype html><html><body>
      <a class="job" href="/jobs/100">Platform Engineer</a>
      <a class="job" href="/jobs/101">Data Engineer</a>
      <a class="nav" href="/about">About us</a>
    </body></html>"""
    page = tmp_path / "careers.html"
    page.write_text(html, encoding="utf-8")

    site = CareerSite(url=page.as_uri(), link_selector="a.job")
    raws = await CareerPagesConnector().fetch_site(site)

    titles = {r.title for r in raws}
    assert titles == {"Platform Engineer", "Data Engineer"}  # .nav anchor excluded
    assert all(r.source == "career_page" for r in raws)
    assert all(r.apply_url.endswith(("/jobs/100", "/jobs/101")) for r in raws)
