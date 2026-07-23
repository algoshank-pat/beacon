"""Filter criteria seed loader — populates filter_settings/filter_keywords once
at first setup. Idempotent: safe to re-run, but only ever *adds* missing rows
(keywords) or upserts scalar settings — it won't remove keywords you've since
added or edited live via the Settings UI."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml

KEYWORD_CATEGORIES = {
    "role_keyword_include",
    "tech_keyword_include",
    "title_exclude",
    "seniority",
    "remote_type",
    "location_include",
    "industries_include",
}


def _serialize(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def load_seed_filters(path: str | Path) -> tuple[dict[str, list[str]], dict[str, object]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    keywords = {k: v for k, v in data.items() if k in KEYWORD_CATEGORIES}
    settings = {k: v for k, v in data.items() if k not in KEYWORD_CATEGORIES}
    return keywords, settings


def seed_filters(conn: sqlite3.Connection, path: str | Path) -> dict[str, int]:
    keywords, settings = load_seed_filters(path)

    keywords_inserted = 0
    for category, terms in keywords.items():
        for term in terms or []:
            existing = conn.execute(
                "SELECT id FROM filter_keywords WHERE category = ? AND keyword = ?",
                (category, term),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO filter_keywords (category, keyword, is_active) VALUES (?, ?, 1)",
                    (category, term),
                )
                keywords_inserted += 1

    settings_upserted = 0
    for key, value in settings.items():
        conn.execute(
            """
            INSERT INTO filter_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (key, _serialize(value)),
        )
        settings_upserted += 1

    conn.commit()
    return {"keywords_inserted": keywords_inserted, "settings_upserted": settings_upserted}
