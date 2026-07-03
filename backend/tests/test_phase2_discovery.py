"""Phase 2 acceptance: connector parsing, normalize, dedup, idempotent storage."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

from app.discovery.base import RawJob  # noqa: E402
from app.discovery.greenhouse import parse_jobs  # noqa: E402
from app.discovery.lever import parse_postings  # noqa: E402
from app.models import Job  # noqa: E402
from app.pipeline.dedup import dedup_hash, is_near_duplicate, normalize_text  # noqa: E402
from app.pipeline.ingest import store_jobs  # noqa: E402
from app.pipeline.normalize import to_job  # noqa: E402


def _session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_greenhouse_parse():
    jobs = [
        {
            "id": 123,
            "title": "Senior Backend Engineer",
            "location": {"name": "Remote - US"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
            "content": "&lt;p&gt;Build &lt;b&gt;APIs&lt;/b&gt;&lt;/p&gt;",
        }
    ]
    raws = parse_jobs("Acme", jobs)
    assert len(raws) == 1
    r = raws[0]
    assert r.company == "Acme"
    assert r.title == "Senior Backend Engineer"
    assert r.remote is True
    assert "Build" in r.description and "<" not in r.description
    assert r.apply_url.endswith("/jobs/123")


def test_lever_parse():
    postings = [
        {
            "id": "abc-1",
            "text": "Platform Engineer",
            "categories": {"location": "New York"},
            "workplaceType": "remote",
            "descriptionPlain": "Run the platform.",
            "applyUrl": "https://jobs.lever.co/acme/abc-1/apply",
            "hostedUrl": "https://jobs.lever.co/acme/abc-1",
        }
    ]
    raws = parse_postings("acme", postings)
    assert len(raws) == 1
    r = raws[0]
    assert r.title == "Platform Engineer"
    assert r.remote is True
    assert r.apply_url.endswith("/apply")


def test_dedup_hash_and_normalize():
    assert normalize_text("  Senior  Engineer!! ") == "senior engineer"
    h1 = dedup_hash("Acme", "Senior Engineer", "Remote")
    h2 = dedup_hash("acme ", " senior  engineer ", "remote")
    assert h1 == h2  # normalization makes these identical


def test_near_duplicate():
    existing = [("Senior Backend Engineer", "Remote - US")]
    # Repost with trivial variation -> near-dup.
    assert is_near_duplicate("Acme", "Senior Backend Engineer", "Remote US", existing)
    # Clearly different role -> not a dup.
    assert not is_near_duplicate("Acme", "Marketing Manager", "London", existing)


def test_to_job_maps_fields():
    raw = RawJob(
        source="greenhouse",
        source_job_id="1",
        company="Acme",
        title="Engineer",
        location="Remote",
        remote=True,
        apply_url="https://x/y",
    )
    job = to_job(raw)
    assert job.company == "Acme"
    assert job.ats_type == "greenhouse"
    assert job.dedup_hash == dedup_hash("Acme", "Engineer", "Remote")


def test_store_jobs_idempotent_and_fuzzy():
    session = _session()
    raws = [
        RawJob(source="greenhouse", source_job_id="1", company="Acme", title="Senior Backend Engineer", location="Remote - US", remote=True),
        RawJob(source="lever", source_job_id="2", company="Beta", title="Data Scientist", location="NYC"),
    ]
    first = store_jobs(session, raws)
    assert first["stored"] == 2
    assert first["deduped"] == 0

    # Re-run with the same two (exact dup) + one near-dup repost of the first.
    raws2 = raws + [
        RawJob(source="greenhouse", source_job_id="3", company="Acme", title="Senior Backend Engineer", location="Remote US", remote=True),
    ]
    second = store_jobs(session, raws2)
    assert second["stored"] == 0
    assert second["deduped"] == 3

    total = len(session.exec(select(Job)).all())
    assert total == 2
