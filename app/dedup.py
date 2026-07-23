"""Fuzzy cross-source dedup: same job posted via two different ingestion sources
(e.g. Adzuna broad discovery AND a company's own targeted ATS board) lands as two
rows with different URLs. This flags the newer one via jobs.duplicate_of_job_id."""
from __future__ import annotations

import sqlite3
from difflib import SequenceMatcher

TITLE_SIMILARITY_THRESHOLD = 0.90
LOCATION_SIMILARITY_THRESHOLD = 0.80


def _normalize(text: str | None) -> str:
    return " ".join((text or "").strip().split()).casefold()


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _locations_match(a: str | None, b: str | None) -> bool:
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return True
    return na == nb or _similarity(na, nb) >= LOCATION_SIMILARITY_THRESHOLD


def find_fuzzy_duplicate(
    conn: sqlite3.Connection,
    company_id: int,
    title: str,
    location: str | None,
) -> int | None:
    """Return the id of an existing job for this company with a near-identical
    title and matching (or unknown) location, or None if no match."""
    normalized_title = _normalize(title)
    rows = conn.execute(
        "SELECT id, title, location FROM jobs WHERE company_id = ? AND status != 'duplicate'",
        (company_id,),
    ).fetchall()

    for row in rows:
        if _similarity(normalized_title, _normalize(row["title"])) >= TITLE_SIMILARITY_THRESHOLD \
                and _locations_match(location, row["location"]):
            return row["id"]
    return None
