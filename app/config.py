"""Application settings and preference loading.

Two layers of configuration:
- Settings (this file's `Settings`): secrets + operational knobs, from environment / .env.
- Preferences (`Preferences`): the user's job-search criteria, from config/preferences.yaml
  (also editable from the dashboard Setup page).
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent
PREFERENCES_PATH = ROOT_DIR / "config" / "preferences.yaml"


class Settings(BaseSettings):
    """Secrets and operational knobs (from environment / .env)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Default backend for both tiers: "hf" | "openai" | "groq".
    # Override per tier with fast_backend / quality_backend.
    llm_backend: str = "hf"
    fast_backend: str = ""      # e.g. "groq" for the high-volume prefilter
    quality_backend: str = ""   # e.g. "groq" or "hf" for tailoring/answers

    # Hugging Face Inference Providers
    hf_token: str = ""
    hf_quality_model: str = "Qwen/Qwen2.5-72B-Instruct"
    hf_fast_model: str = "meta-llama/Llama-3.1-8B-Instruct"
    hf_provider: str = "auto"

    # Groq (free tier, OpenAI-compatible)
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_quality_model: str = "llama-3.3-70b-versatile"
    groq_fast_model: str = "llama-3.1-8b-instant"

    # Generic OpenAI-compatible backend (Ollama, Gemini, OpenRouter, OpenAI, ...)
    openai_base_url: str = ""        # e.g. http://localhost:11434/v1
    openai_api_key: str = ""
    openai_quality_model: str = ""
    openai_fast_model: str = ""

    def resolve_tier(self, tier: str) -> tuple[str, str, str, str]:
        """Return (kind, model, base_url, api_key) for tier in {"fast","quality"}.

        `kind` is "openai" (any OpenAI-compatible API) or "hf".
        """
        is_fast = tier == "fast"
        backend = (self.fast_backend if is_fast else self.quality_backend) or self.llm_backend
        if backend == "groq":
            model = self.groq_fast_model if is_fast else self.groq_quality_model
            return "openai", model, self.groq_base_url, self.groq_api_key
        if backend == "openai":
            model = self.openai_fast_model if is_fast else self.openai_quality_model
            return "openai", model, self.openai_base_url, self.openai_api_key
        model = self.hf_fast_model if is_fast else self.hf_quality_model
        return "hf", model, "", ""

    # Adzuna job search API (optional; free app id/key from developer.adzuna.com)
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    adzuna_country: str = "us"

    database_url: str = "sqlite:///./job_applier.db"

    # Safety / autonomy
    dry_run: bool = True
    submit_kill_switch: bool = False
    daily_submit_cap: int = 40
    match_threshold: int = 70

    # Orchestrator
    cycle_interval_minutes: int = 60

    # Assisted hand-off (captcha-gated apps): one persistent browser, streamed
    # into the dashboard (headless — no pop-up window; the stream is the view).
    assist_user_data_dir: str = "./.assist_profile"
    assist_headless: bool = True

    # SMTP
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    summary_email_to: str = ""

    data_dir: Path = ROOT_DIR / "data"

    @property
    def resumes_dir(self) -> Path:
        return self.data_dir / "resumes"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.resumes_dir, self.artifacts_dir):
            d.mkdir(parents=True, exist_ok=True)


class RemotePreference(str, Enum):
    remote_only = "remote_only"
    hybrid_ok = "hybrid_ok"
    onsite_ok = "onsite_ok"
    any = "any"


class CareerSite(BaseModel):
    """A generic career page to scrape (brittle; opt-in)."""

    url: str
    link_selector: str = "a"  # CSS selector for job links
    keyword: str = ""          # optional filter on link text/href


class SourceConfig(BaseModel):
    enabled: bool = False
    autonomy: bool = False
    companies: list[str] = Field(default_factory=list)
    sites: list[CareerSite] = Field(default_factory=list)
    # Search aggregators: optional category filter (mainly for The Muse) and a
    # cap on how many result pages to pull per query.
    categories: list[str] = Field(default_factory=list)
    max_pages: int = 1


class Preferences(BaseModel):
    """User's job-search criteria. Mirrors config/preferences.yaml."""

    desired_roles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_preference: RemotePreference = RemotePreference.any
    min_salary: int | None = None
    salary_currency: str = "USD"
    require_sponsorship: bool = False
    work_authorization: str = ""
    # When true, discovery keeps only jobs whose TITLE contains one of
    # desired_roles (whole-word match). Stops full ATS boards flooding the pool.
    discovery_title_filter: bool = True
    # Titles containing any of these whole words are dropped at discovery
    # (e.g. seniority levels above your target, or off-domain roles).
    exclude_keywords: list[str] = Field(default_factory=list)
    sources: dict[str, SourceConfig] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = PREFERENCES_PATH) -> "Preferences":
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    def save(self, path: Path = PREFERENCES_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings


def get_preferences() -> Preferences:
    """Load fresh each call so dashboard edits take effect without restart."""
    return Preferences.load()
