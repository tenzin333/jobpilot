"""Discovery ingestion: fetch from enabled sources, normalize, dedup, store.

Returns counts the orchestrator records on a Run.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import Preferences
from app.discovery.adzuna import AdzunaConnector
from app.discovery.ashby import AshbyConnector
from app.discovery.base import Connector, RawJob
from app.discovery.career_pages import CareerPagesConnector
from app.discovery.greenhouse import GreenhouseConnector
from app.discovery.lever import LeverConnector
from app.discovery.remotive import RemotiveConnector
from app.discovery.smartrecruiters import SmartRecruitersConnector
from app.discovery.themuse import TheMuseConnector
from app.discovery.workable import WorkableConnector
from app.discovery.util import relevant_title
from app.models import AtsType, Job
from app.pipeline.dedup import dedup_hash, is_near_duplicate
from app.pipeline.normalize import to_job

log = logging.getLogger(__name__)

# Company-slug based ATS connectors.
CONNECTORS: dict[str, Connector] = {
    AtsType.greenhouse.value: GreenhouseConnector(),
    AtsType.lever.value: LeverConnector(),
    AtsType.ashby.value: AshbyConnector(),
    AtsType.smartrecruiters.value: SmartRecruitersConnector(),
    AtsType.workable.value: WorkableConnector(),
}

# Keyword-search aggregators (query from preferences, not per-company).
SEARCH_CONNECTORS = {
    AtsType.themuse.value: TheMuseConnector(),
    AtsType.remotive.value: RemotiveConnector(),
    AtsType.adzuna.value: AdzunaConnector(),
}


_CAREER = CareerPagesConnector()


async def fetch_all(prefs: Preferences) -> list[RawJob]:
    """Fetch raw jobs from every enabled source CONCURRENTLY.

    Each unit of work (one company fetch, one search connector, one career site)
    runs as its own task; one slow/failing source can't stall or block the rest.
    """
    tasks: list[tuple[str, "asyncio.Future"]] = []

    for source_name, source_cfg in prefs.sources.items():
        if not source_cfg.enabled:
            continue

        connector = CONNECTORS.get(source_name)
        if connector is not None:
            for company in source_cfg.companies:
                tasks.append((f"{source_name}:{company}", connector.fetch(company)))
            continue

        search = SEARCH_CONNECTORS.get(source_name)
        if search is not None:
            tasks.append((source_name, search.fetch_jobs(prefs, source_cfg)))
            continue

        if source_name == AtsType.career_page.value:
            for site in source_cfg.sites:
                tasks.append((f"career_page:{site.url}", _CAREER.fetch_site(site)))

    raws: list[RawJob] = []
    if tasks:
        results = await asyncio.gather(*(t[1] for t in tasks), return_exceptions=True)
        for (label, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                log.warning("discovery source failed (%s): %s", label, result)
            else:
                raws.extend(result)

    # Central relevance gate: keep role-matching titles, drop excluded ones
    # (seniority levels above target, off-domain roles). Board connectors
    # otherwise return the entire company board.
    if prefs.discovery_title_filter:
        include = prefs.desired_roles
        exclude = prefs.exclude_keywords
        raws = [r for r in raws if relevant_title(r.title, include, exclude)]
    log.info("discovery fetched %d jobs from %d source-tasks", len(raws), len(tasks))
    return raws


def store_jobs(session: Session, raws: list[RawJob]) -> dict[str, int]:
    """Normalize + dedup + persist. Returns {discovered, deduped, stored}."""
    existing_hashes = set(session.exec(select(Job.dedup_hash)).all())

    # company(normalized) -> list of (title, location) for fuzzy near-dup checks.
    by_company: dict[str, list[tuple[str, str]]] = {}
    for company, title, location in session.exec(
        select(Job.company, Job.title, Job.location)
    ).all():
        by_company.setdefault(company.lower().strip(), []).append((title, location))

    stored = 0
    deduped = 0
    for raw in raws:
        h = dedup_hash(raw.company, raw.title, raw.location)
        if h in existing_hashes:
            deduped += 1
            continue
        company_key = raw.company.lower().strip()
        if is_near_duplicate(raw.company, raw.title, raw.location, by_company.get(company_key, [])):
            deduped += 1
            continue

        # Commit per row so a concurrent run inserting the same dedup_hash
        # (unique constraint) can't fail and discard the whole batch.
        session.add(to_job(raw))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            deduped += 1
            continue
        existing_hashes.add(h)
        by_company.setdefault(company_key, []).append((raw.title, raw.location))
        stored += 1

    return {"discovered": len(raws), "deduped": deduped, "stored": stored}


async def discover_and_store(session: Session, prefs: Preferences) -> dict[str, int]:
    raws = await fetch_all(prefs)
    return store_jobs(session, raws)
