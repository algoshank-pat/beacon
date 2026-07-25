"""Environment/settings loading for the job search app."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


def _sqlite_url_to_path(url: str) -> Path:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise ValueError(f"Unsupported DATABASE_URL scheme: {url!r} (expected sqlite:///...)")
    return Path(url[len(prefix):])


def _resolve_llm_provider() -> str:
    """LLM_PROVIDER if set explicitly; otherwise infer from whichever single
    API key is actually present, so a user who only ever added a
    GEMINI_API_KEY isn't stuck erroring on the default's ANTHROPIC_API_KEY
    requirement. Both-or-neither key present still defaults to "anthropic"."""
    explicit = os.environ.get("LLM_PROVIDER")
    if explicit:
        return explicit
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    if has_gemini and not has_anthropic:
        return "gemini"
    return "anthropic"


@dataclass(frozen=True)
class Settings:
    database_path: Path = field(default_factory=lambda: _sqlite_url_to_path(
        os.environ.get("DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'job_search.db'}")
    ))
    anthropic_api_key: str | None = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY"))
    gemini_api_key: str | None = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY"))
    # Which provider's models actually get called for visa classification and
    # fit scoring -- "anthropic" (default, Haiku + Sonnet) or "gemini"
    # (Flash-Lite + Pro). Everything else about the pipeline (schemas,
    # thresholds, budget tracking) is identical either way; see
    # app.llm_provider for the one place that reads this.
    llm_provider: str = field(default_factory=_resolve_llm_provider)
    adzuna_app_id: str | None = field(default_factory=lambda: os.environ.get("ADZUNA_APP_ID"))
    adzuna_app_key: str | None = field(default_factory=lambda: os.environ.get("ADZUNA_APP_KEY"))
    google_sheet_id: str | None = field(default_factory=lambda: os.environ.get("GOOGLE_SHEET_ID"))
    google_job_log_sheet_id: str | None = field(default_factory=lambda: os.environ.get("GOOGLE_JOB_LOG_SHEET_ID"))
    google_sheets_credentials_path: str | None = field(
        default_factory=lambda: os.environ.get("GOOGLE_SHEETS_CREDENTIALS_PATH")
    )
    claude_desktop_project_id: str | None = field(
        default_factory=lambda: os.environ.get("CLAUDE_DESKTOP_PROJECT_ID")
    )
    fmp_api_key: str | None = field(default_factory=lambda: os.environ.get("FMP_API_KEY"))
    startuphub_api_key: str | None = field(default_factory=lambda: os.environ.get("STARTUPHUB_API_KEY"))
    tinyfish_api_key: str | None = field(default_factory=lambda: os.environ.get("TINYFISH_API_KEY"))


def get_settings() -> Settings:
    return Settings()
