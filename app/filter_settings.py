"""Typed access to live filter_settings/filter_keywords."""
from __future__ import annotations

import sqlite3

_BOOL_KEYS = {
    "require_visa_sponsorship",
    "require_h1b_track_record",
    "require_us_location",
    "remote_only",
    "generate_cover_letter",
    "pipeline_paused",
    "job_log_enabled",
}
_INT_KEYS = {
    "fit_score_threshold",
    "employee_count_min",
    "employee_count_max",
    "founded_after_year",
    "approval_reminder_hours",
    "approval_poll_interval_minutes",
    "approval_poller_max_consecutive_failures",
    "posted_within_days",
    "daily_enrichment_limit",
    "link_check_batch_size",
    "salary_refresh_batch_size",
    "cloud_platforms_refresh_batch_size",
}
_FLOAT_KEYS = {"daily_token_budget", "monthly_token_budget"}


def _coerce(key: str, raw: str | None):
    if raw is None:
        return None
    if key in _BOOL_KEYS:
        return raw.lower() == "true"
    if key in _INT_KEYS:
        return int(raw)
    if key in _FLOAT_KEYS:
        return float(raw)
    return raw


def get_filter_settings(conn: sqlite3.Connection) -> dict[str, object]:
    rows = conn.execute("SELECT key, value FROM filter_settings").fetchall()
    return {row["key"]: _coerce(row["key"], row["value"]) for row in rows}


def get_active_keywords(conn: sqlite3.Connection, category: str) -> list[str]:
    rows = conn.execute(
        "SELECT keyword FROM filter_keywords WHERE category = ? AND is_active = 1",
        (category,),
    ).fetchall()
    return [row["keyword"] for row in rows]
