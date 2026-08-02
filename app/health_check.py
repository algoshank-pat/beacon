"""Periodic sanity checks for silent data-lifecycle drift -- the kind that
doesn't crash anything or show up in a normal pipeline run's return dict,
so nothing surfaces it until a human happens to notice (a live incident:
443 visa-restricted jobs sat on Beacon, and the Job Log grew to 28k rows,
both unnoticed for a while since neither failure raises an exception
anywhere). Meant to run on its own daily schedule (see
app.scheduler.run_health_check_job) and just log a warning per finding --
this module never fixes anything itself, only surfaces drift early instead
of waiting for someone to eyeball row counts."""
from __future__ import annotations

import sqlite3

from app.job_log import JOB_ID_COL_INDEX as JOB_LOG_JOB_ID_COL_INDEX
from app.sheets_retry import call_with_retry

JOB_LOG_ROW_WARNING_THRESHOLD = 5_000


def check_health(conn: sqlite3.Connection, job_log_ws=None) -> dict:
    """Returns a dict of findings; an empty-list value means that check is
    clean. Never raises on a Sheets outage -- a health check that itself
    crashes defeats the purpose (see app.scheduler.run_health_check_job for
    the try/except this still relies on around the whole job, but this
    function additionally treats "can't read the Job Log" as a data point
    -- job_log_row_count: None -- rather than an error)."""
    stuck_restricted = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE visa_flag = 'restricted' AND sheet_row_number IS NOT NULL"
    ).fetchone()[0]

    job_log_row_count = None
    if job_log_ws is not None:
        try:
            values = call_with_retry(job_log_ws.col_values, JOB_LOG_JOB_ID_COL_INDEX)
            job_log_row_count = max(0, len(values) - 1)  # exclude header row
        except Exception:  # noqa: BLE001 -- a Sheets outage here is itself just a finding, not a crash
            job_log_row_count = None

    return {
        "stuck_restricted_on_beacon": stuck_restricted,
        "job_log_row_count": job_log_row_count,
    }
