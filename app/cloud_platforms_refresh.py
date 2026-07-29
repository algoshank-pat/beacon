"""Cloud Platforms Refresh — for Beacon jobs whose stored description is
truncated (Adzuna's 500-char preview cap) before any AWS/GCP/Azure mention
would appear. Re-checks using the full posting page instead.

Real gap found live, reported directly: comparing AWS vs GCP vs Azure
mention counts surfaced that most jobs have no cloud platform label at all
-- confirmed 87.8% of Adzuna-sourced jobs on Beacon have a description
truncated to exactly 500 characters, hiding any mention past that cutoff.
Directly-tracked companies (Greenhouse/Lever/Ashby) already get the real
full description at ingest time (0% truncated, confirmed live) and don't
need this.

Not every job's full page is actually fetchable this way -- same JS-
redirect limitation already documented in app.salary_refresh/app.link_check
(some Adzuna URLs resolve server-side to real page text, others are a
client-side JS redirect `requests` can't follow). A job whose page can't be
fetched, or whose fetched page still mentions nothing, is simply left as
None; `cloud_platforms_checked_at` is stamped either way so it isn't
retried forever.

Sheets writes are batched the same way as app.salary_refresh (see its
module docstring for the full reasoning): the row lookup reads the Job ID
column ONCE via build_job_row_map instead of once per job, and every job's
Cloud Platforms update is collected and flushed via a single
batch_update_cells call at the end, instead of one API call per job."""
from __future__ import annotations

import sqlite3

from app.cloud_platforms import resolve_cloud_platforms
from app.salary_extraction import fetch_job_page_text
from app.sheets import batch_update_cells, build_job_row_map, cloud_platforms_update_request


def run_cloud_platforms_refresh(
    conn: sqlite3.Connection, limit: int | None = None, main_ws=None,
) -> dict:
    """Re-attempts cloud-platform detection (via the full posting page) for
    jobs currently on Beacon whose stored description is exactly 500
    characters (Adzuna's truncation cap) and haven't been checked yet.
    Jobs with a full (non-truncated) description are never re-checked --
    the free ingest-time result already saw everything there was to see. No
    external API quota to respect here (same reasoning as
    app.link_check's batch_size) -- `limit` exists purely to bound how much
    latency one pipeline run takes on."""
    query = """
        SELECT * FROM jobs
        WHERE sheet_row_number IS NOT NULL
          AND cloud_platforms_checked_at IS NULL
          AND LENGTH(description) = 500
        ORDER BY id
    """
    if limit is not None:
        query += " LIMIT ?"
        jobs = conn.execute(query, (limit,)).fetchall()
    else:
        jobs = conn.execute(query).fetchall()

    row_map = build_job_row_map(main_ws) if main_ws is not None else {}

    checked = found = 0
    sheet_updates = []
    for job in jobs:
        try:
            full_text = fetch_job_page_text(job["url"])
        except Exception:  # noqa: BLE001 -- unfetchable page (JS redirect, dead link, network error) is expected, not fatal
            full_text = None

        platforms = resolve_cloud_platforms(f"{job['title']}\n{full_text}") if full_text else None

        conn.execute(
            "UPDATE jobs SET cloud_platforms_checked_at = CURRENT_TIMESTAMP, "
            "cloud_platforms = COALESCE(?, cloud_platforms) WHERE id = ?",
            (platforms, job["id"]),
        )
        conn.commit()
        checked += 1

        if platforms is not None:
            found += 1
            row_number = row_map.get(job["id"])
            if row_number is not None:
                sheet_updates.append(cloud_platforms_update_request(row_number, platforms))

    if main_ws is not None:
        batch_update_cells(main_ws, sheet_updates)

    return {"checked": checked, "found": found}
