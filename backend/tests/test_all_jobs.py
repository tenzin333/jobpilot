from __future__ import annotations

from datetime import date
import json

import pandas as pd

from app.discovery.all_jobs import JOB_SITES, parse_jobs


def test_parse_jobs_produces_json_safe_raw_payload() -> None:
    batch = pd.DataFrame(
        [
            {
                "id": "li-123",
                "site": "linkedin",
                "title": "AI Engineer",
                "company": "Example",
                "job_url": "https://example.com/jobs/123",
                "date_posted": date(2026, 9, 24),
            }
        ]
    )

    jobs = parse_jobs([batch])

    assert len(jobs) == 1
    assert jobs[0].raw["date_posted"] == "2026-09-24"
    json.dumps(jobs[0].raw)


def test_job_sites_omit_broken_bdjobs_adapter() -> None:
    assert "bdjobs" not in JOB_SITES
    assert "linkedin" in JOB_SITES
