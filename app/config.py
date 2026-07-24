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


@dataclass(frozen=True)
class Settings:
    database_path: Path = field(default_factory=lambda: _sqlite_url_to_path(
        os.environ.get("DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'job_search.db'}")
    ))
    anthropic_api_key: str | None = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY"))
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
