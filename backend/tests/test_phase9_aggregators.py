"""Aggregator connectors: pure parsers + keyword filtering."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.discovery.adzuna import parse_jobs as parse_adzuna  # noqa: E402
from app.discovery.linkedin import parse_jobs as parse_linkedin  # noqa: E402
from app.discovery.remotive import _parse_salary  # noqa: E402
from app.discovery.remotive import parse_jobs as parse_remotive  # noqa: E402
from app.discovery.themuse import parse_jobs as parse_muse  # noqa: E402
from app.discovery.util import is_remote, matches_any, relevant_title, strip_html  # noqa: E402


def test_util_helpers():
    cleaned = strip_html("<p>Hello &amp; <b>world</b></p>")
    assert "Hello" in cleaned and "world" in cleaned and "<" not in cleaned
    assert matches_any("Senior GenAI Engineer", ["gen ai", "genai"]) is True
    assert matches_any("Marketing Manager", ["gen ai", "llm"]) is False
    assert matches_any("anything", []) is True  # no keywords -> keep
    assert is_remote("Remote - US", "Engineer") is True


def test_relevant_title_include_and_exclude():
    include = ["ai", "machine learning"]
    exclude = ["senior", "architect", "editor", "video"]
    # Mid-level AI/ML roles kept.
    assert relevant_title("AI Engineer", include, exclude) is True
    assert relevant_title("Machine Learning Engineer", include, exclude) is True
    # The four roles the user flagged are dropped.
    assert relevant_title("Senior Independent AI Engineer / Architect", include, exclude) is False
    assert relevant_title("Mid/Senior AI Cinematic Video Editor", include, exclude) is False
    assert relevant_title("Senior Architect - Enterprise AI HLD Architect", include, exclude) is False
    assert relevant_title("Senior Applied AI Solutions Consultant", include, exclude) is False
    # Non-AI role dropped by include check.
    assert relevant_title("Office Manager", include, exclude) is False


def test_muse_parse_filters_by_keyword():
    jobs = [
        {"id": 1, "name": "Generative AI Engineer", "company": {"name": "Acme"},
         "locations": [{"name": "Remote"}], "contents": "<p>Build LLMs</p>",
         "refs": {"landing_page": "https://muse/1"}},
        {"id": 2, "name": "Office Manager", "company": {"name": "Acme"},
         "locations": [{"name": "NYC"}], "contents": "<p>Manage office</p>",
         "refs": {"landing_page": "https://muse/2"}},
    ]
    raws = parse_muse(jobs, keywords=["generative ai", "llm"])
    assert len(raws) == 1
    assert raws[0].title == "Generative AI Engineer"
    assert raws[0].source == "themuse"
    assert raws[0].remote is True
    assert "<" not in raws[0].description


def test_remotive_parse_and_salary():
    assert _parse_salary("$120k - $150k") == (120000, 150000)
    assert _parse_salary("") == (None, None)
    jobs = [{
        "id": 9, "title": "LLM Engineer", "company_name": "Beta",
        "candidate_required_location": "Worldwide", "salary": "$140k - $180k",
        "description": "<p>Train models</p>", "url": "https://remotive/9",
    }]
    raws = parse_remotive(jobs)
    assert len(raws) == 1
    r = raws[0]
    assert r.source == "remotive" and r.remote is True
    assert r.salary_min == 140000 and r.salary_max == 180000
    assert r.apply_url.endswith("/9")


def test_linkedin_parse_card():
    fragment = """
    <ul>
      <li>
        <div class="base-card" data-entity-urn="urn:li:jobPosting:3812345678">
          <a class="base-card__full-link absolute top-0 right-0 p-0 z-[2]" href="https://www.linkedin.com/jobs/view/ai-engineer-at-acme-3812345678?trk=x">link</a>
          <h3 class="base-search-card__title"> AI Engineer </h3>
          <h4 class="base-search-card__subtitle"><a href="/company/acme">Acme AI</a></h4>
          <span class="job-search-card__location"> Remote, United States </span>
        </div>
      </li>
      <li><div class="base-card">no link, skipped</div></li>
    </ul>
    """
    raws = parse_linkedin(fragment)
    assert len(raws) == 1
    r = raws[0]
    assert r.source == "linkedin"
    assert r.title == "AI Engineer"
    assert r.company == "Acme AI"
    assert r.source_job_id == "3812345678"
    assert r.remote is True
    assert r.apply_url.endswith("3812345678")  # query string stripped


def test_adzuna_parse_with_salary():
    results = [{
        "id": "a1", "title": "Machine Learning Engineer",
        "company": {"display_name": "Gamma"}, "location": {"display_name": "Remote, US"},
        "salary_min": 150000.0, "salary_max": 200000.0,
        "description": "Build ML systems", "redirect_url": "https://adzuna/a1",
    }]
    raws = parse_adzuna(results)
    assert len(raws) == 1
    r = raws[0]
    assert r.source == "adzuna"
    assert r.salary_min == 150000 and r.salary_max == 200000
    assert r.remote is True
