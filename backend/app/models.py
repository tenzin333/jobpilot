r"""SQLModel tables: Profile, Job, Application, Run.

Status flow for Application:
    ranked -> tailored -> queued -> submitted
                                 \-> needs_human
                                 \-> failed
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import Column, UniqueConstraint
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ApplicationStatus(str, Enum):
    ranked = "ranked"
    tailored = "tailored"
    queued = "queued"
    submitted = "submitted"
    needs_human = "needs_human"
    failed = "failed"


class AtsType(str, Enum):
    greenhouse = "greenhouse"
    lever = "lever"
    ashby = "ashby"
    smartrecruiters = "smartrecruiters"
    workable = "workable"
    career_page = "career_page"
    # Search aggregators (keyword-based; apply links are external -> needs_human).
    themuse = "themuse"
    remotive = "remotive"
    adzuna = "adzuna"
    linkedin = "linkedin"


class Profile(SQLModel, table=True):
    """The user's parsed base resume + search preferences snapshot.

    There is normally a single Profile row (id=1). Structured fields are stored
    as JSON; `preferences` mirrors config/preferences.yaml at parse time.
    """

    id: int | None = Field(default=None, primary_key=True)
    full_name: str = ""
    honorific: str = ""
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    base_resume_path: str = ""
    raw_text: str = ""
    experience: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    skills: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    education: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    preferences: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # Common application answers (work auth, links, salary, EEO, screening Qs)
    # used to fill arbitrary form fields. See app/llm/answers.py.
    answer_bank: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Job(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("dedup_hash", name="uq_job_dedup_hash"),)

    id: int | None = Field(default=None, primary_key=True)
    source: str = ""  # AtsType value
    source_job_id: str = ""
    company: str = ""
    title: str = ""
    location: str = ""
    remote: bool = False
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str = "USD"
    description: str = ""
    apply_url: str = ""
    ats_type: str = ""  # AtsType value
    dedup_hash: str = Field(index=True)
    discovered_at: datetime = Field(default_factory=utcnow)
    raw: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Application(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("job_id", name="uq_application_job_id"),)

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    status: str = Field(default=ApplicationStatus.ranked.value, index=True)
    match_score: int | None = None
    score_rationale: str = ""
    gaps: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    resume_path: str = ""
    cover_letter_path: str = ""
    needs_human_reason: str = ""
    submitted_at: datetime | None = None
    events: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Control(SQLModel, table=True):
    """Single-row runtime controls, editable from the Settings page.

    Seeded from env Settings on first use; overrides them thereafter so the
    kill switch / DRY_RUN / cap / threshold can be changed without a restart.
    """

    id: int | None = Field(default=None, primary_key=True)
    dry_run: bool = True
    submit_kill_switch: bool = False
    daily_submit_cap: int = 40
    match_threshold: int = 70
    scheduler_enabled: bool = False
    cycle_interval_minutes: int = 60
    updated_at: datetime = Field(default_factory=utcnow)


class ScoreCache(SQLModel, table=True):
    """Cached LLM match scores, keyed by a hash of (kind, model, profile, job).

    Lets Discover/Re-score reuse a score whose inputs (the job text, the profile,
    and the model) are unchanged, instead of re-calling the LLM — which keeps
    re-runs off the free-tier rate limit. A changed résumé, job description, or
    model produces a different key, so it recomputes automatically.
    """

    key: str = Field(primary_key=True)
    kind: str = Field(index=True)  # "prefilter" | "deep"
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class Run(SQLModel, table=True):
    """Per-orchestrator-cycle stats, used for daily summaries."""

    id: int | None = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    discovered: int = 0
    deduped: int = 0
    ranked: int = 0
    tailored: int = 0
    submitted: int = 0
    needs_human: int = 0
    failed: int = 0
    notes: str = ""
