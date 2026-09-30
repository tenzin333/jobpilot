import asyncio
from datetime import date, datetime
from enum import Enum
import logging
import math
from typing import Any

from app.config import Preferences, SourceConfig
from app.discovery.base import RawJob
from app.discovery.util import _scrape_sync
from app.models import AtsType

log = logging.getLogger(__name__)

# JobSpy 1.1.82's BDJobs adapter crashes because its constructor does not
# accept the user_agent argument that JobSpy passes to every scraper.
JOB_SITES = [
    "linkedin",
    "indeed",
    "zip_recruiter",
    "glassdoor",
    "google",
    "bayt",
    "naukri",
]

site_names = ""
search_term = "ai engineer , remote"
google_search_term = "ai engineer , remote"
location = "worldwide"
job_type = "full time"
is_remote = True
hours_old = 72


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return str(value)



def parse_jobs(batches: list[Any]) -> list[RawJob]:
    
    results: list[RawJob] = []
    rows = []
    
    for batch in batches:
        if isinstance(batch, Exception):
            log.warning("AllJobs query failed: %s", batch)
            continue
        rows.extend(batch.astype(object).where(batch.notna(), None).to_dict(orient="records"))
    
    for row in rows:
        source_job_id = str(row.get("id") or "").strip()
        title = str(row.get("title") or "").strip()
        company = str(row.get("company") or "").strip()
        apply_url = str(
            row.get("job_url_direct")
            or row.get("job_url")
            or ""
        ).strip()

        if not source_job_id or not title or not company or not apply_url:
            continue

        results.append(
            RawJob(
                source=AtsType.all_jobs.value,
                source_job_id=source_job_id,
                title=title,
                company=company,
                apply_url=apply_url,
                location=str(row.get("location") or ""),
                remote=bool(row.get("is_remote")),
                salary_min=None,
                salary_max=None,
                salary_currency=str(row.get("currency") or "USD"),
                description=str(row.get("description") or ""),
                raw=_json_safe(row),
            )
        )
    
    return results
        
     
class AllJobsConnector:
    name = AtsType.all_jobs.value
    
    async def fetch_jobs(self, prefs: Preferences, cfg: SourceConfig) -> list[RawJob]:
        queries = prefs.desired_roles or [""]
        where = prefs.locations[0] if prefs.locations else ""
        batches = await asyncio.gather(
                *(self.scrape_one(query, where) for query in queries),
                return_exceptions=True
        )
        return parse_jobs(batches)
        
        # jobs = scrape_jobs(
        #         site_name=["indeed", "linkedin", "remoteok"],
        #         search_term="ai engineer remote",
        #         location="United States, India",
        #         results_wanted=20,
        #         hours_old=168,
        #         is_remote=True,
        #         description_format="markdown"               
        #     )
                
    
    async def scrape_one(self, query: str, where:str):
        kwargs = {
            "site_name": JOB_SITES,
            "search_term": query,
            "is_remote": is_remote,
            "results_wanted": 20,
            "hours_old": 72,
        }
        return await asyncio.to_thread(_scrape_sync, kwargs)

       
