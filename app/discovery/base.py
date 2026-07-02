"""Connector protocol and the common RawJob shape all connectors emit."""
from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class RawJob(BaseModel):
    """Source-agnostic job as emitted by a connector, pre-normalization."""

    source: str  # AtsType value
    source_job_id: str
    company: str
    title: str
    location: str = ""
    remote: bool = False
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str = "USD"
    description: str = ""
    apply_url: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


class Connector(Protocol):
    """A source connector. `name` is the AtsType value."""

    name: str

    async def fetch(self, company: str) -> list[RawJob]: ...
