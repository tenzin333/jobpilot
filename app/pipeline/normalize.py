"""Map a source-agnostic RawJob into a persistable Job (with dedup hash)."""
from __future__ import annotations

from app.discovery.base import RawJob
from app.models import Job
from app.pipeline.dedup import dedup_hash


def to_job(raw: RawJob) -> Job:
    return Job(
        source=raw.source,
        source_job_id=raw.source_job_id,
        company=raw.company.strip(),
        title=raw.title.strip(),
        location=raw.location.strip(),
        remote=raw.remote,
        salary_min=raw.salary_min,
        salary_max=raw.salary_max,
        salary_currency=raw.salary_currency,
        description=raw.description,
        apply_url=raw.apply_url,
        ats_type=raw.source,
        dedup_hash=dedup_hash(raw.company, raw.title, raw.location),
        raw=raw.raw,
    )
