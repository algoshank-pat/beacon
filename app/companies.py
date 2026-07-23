"""Company lookup/auto-creation during ingestion."""
from __future__ import annotations

import sqlite3


def _normalize(name: str) -> str:
    return " ".join(name.strip().split()).casefold()


def get_or_create_company(conn: sqlite3.Connection, name: str, source_type: str) -> int:
    """Look up a company by case/whitespace-insensitive name match; auto-create if new.

    Companies seeded manually (or found in a prior poll) always win the lookup — this
    only creates a new row when no existing company matches, per
    "seed list I maintain + auto-added from postings".
    """
    normalized = _normalize(name)
    row = conn.execute("SELECT id, name FROM companies").fetchall()
    for existing in row:
        if _normalize(existing["name"]) == normalized:
            return existing["id"]

    cursor = conn.execute(
        "INSERT INTO companies (name, source_type) VALUES (?, ?)",
        (name.strip(), source_type),
    )
    conn.commit()
    return cursor.lastrowid
