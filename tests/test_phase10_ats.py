"""Ashby / SmartRecruiters / Workable connector pure parsers."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.discovery.ashby import parse_jobs as parse_ashby  # noqa: E402
from app.discovery.smartrecruiters import parse_jobs as parse_sr  # noqa: E402
from app.discovery.workable import parse_jobs as parse_workable  # noqa: E402


def test_ashby_parse():
    jobs = [{
        "id": "j1", "title": "AI Engineer", "location": "Remote",
        "isRemote": True, "descriptionPlain": "Build agents.",
        "applyUrl": "https://jobs.ashbyhq.com/acme/j1/application",
        "jobUrl": "https://jobs.ashbyhq.com/acme/j1",
    }]
    raws = parse_ashby("acme", jobs)
    assert len(raws) == 1
    r = raws[0]
    assert r.source == "ashby" and r.title == "AI Engineer"
    assert r.remote is True
    assert r.apply_url.endswith("/application")


def test_smartrecruiters_parse():
    content = [{
        "id": "100", "name": "Machine Learning Engineer",
        "company": {"identifier": "acme", "name": "Acme"},
        "location": {"city": "Berlin", "country": "Germany", "remote": False},
    }]
    raws = parse_sr("acme", content)
    assert len(raws) == 1
    r = raws[0]
    assert r.source == "smartrecruiters" and r.company == "Acme"
    assert r.location == "Berlin, Germany"
    assert r.apply_url == "https://jobs.smartrecruiters.com/acme/100"


def test_workable_parse():
    jobs = [{
        "shortcode": "ABC123", "title": "LLM Engineer",
        "location": {"city": "Remote", "country": "US", "telecommuting": True},
        "description": "<p>Train models</p>",
        "application_url": "https://apply.workable.com/acme/j/ABC123/apply",
        "url": "https://acme.workable.com/jobs/ABC123",
    }]
    raws = parse_workable("Acme", jobs)
    assert len(raws) == 1
    r = raws[0]
    assert r.source == "workable" and r.title == "LLM Engineer"
    assert r.remote is True
    assert "<" not in r.description
    assert r.apply_url.endswith("/apply")
