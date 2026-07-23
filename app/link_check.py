"""Link Health Check — for jobs on Beacon that app.ingest.detect_closed_jobs
can't cover: Adzuna-sourced postings (78% of Beacon, confirmed live) and
any other non-targeted-board source. Adzuna's API is a bounded keyword
search, not a "list all this employer's open jobs" endpoint, so there's no
way to know a specific posting closed just by re-querying Adzuna the way
detect_closed_jobs re-polls Greenhouse/Lever/Ashby and diffs the result.

Instead, periodically re-fetches each such job's own URL directly.
Confirmed live against real Adzuna listings: a still-open posting's
`/land/ad/...` URL redirects (303) to `/details/...` and returns HTTP 200;
a closed one also redirects to `/details/...` but returns HTTP 404 --
*and* renders a full HTML page (for SEO) containing the exact banner text
"Unfortunately, this job is no longer available". Both signals are checked
(the specific phrase primarily, since it's unambiguous; the raw 404/410
status as a fallback, since a non-Adzuna URL that's genuinely gone won't
have Adzuna's specific wording). Deliberately NOT matching a broader set of
"closed"-sounding phrases -- job boards phrase this too many different
ways, and a broad text heuristic risks evicting a posting that's actually
still open. A network error, timeout, or any other inconclusive result is
treated as "assume still open" -- link_checked_at is still stamped (so this
job isn't re-checked again immediately), but nothing is evicted, since
wrongly evicting a live posting is worse than leaving a dead one visible a
little longer.
"""
from __future__ import annotations

import sqlite3

from app.html_strip import strip_html
from app.http_client import BROWSER_HEADERS, RequestFailedError, request_with_retry
from app.job_log import STAGE_CLOSED, upsert_job_log_row
from app.observability import log_step
from app.sheets import remove_main_row

_DEAD_STATUS_CODES = {404, 410}
_ADZUNA_CLOSED_PHRASE = "this job is no longer available"


def check_link_dead(url: str, session=None) -> bool | None:
    """Returns True if the posting is confirmed closed, False if confirmed
    still live, or None if the check was inconclusive (network error,
    timeout). Callers must treat None the same as False -- never evict on
    an inconclusive result."""
    # Only forward session when a caller (tests) actually supplies one --
    # passing session=None explicitly overrides request_with_retry's own
    # default (session=requests), crashing on None.request(...). This is a
    # real bug that was crashing this step on the first job of every run.
    kwargs = {"session": session} if session is not None else {}
    try:
        response = request_with_retry("GET", url, headers=BROWSER_HEADERS, timeout=15, **kwargs)
    except RequestFailedError:
        return None

    text = strip_html(response.text).lower()
    if _ADZUNA_CLOSED_PHRASE in text:
        return True
    if response.status_code in _DEAD_STATUS_CODES:
        return True
    return False


def run_link_check(
    conn: sqlite3.Connection, limit: int | None = None, workflow_run_id: int | None = None,
    main_ws=None, job_log_ws=None, session=None,
) -> dict:
    """Re-verifies jobs currently on Beacon that app.ingest.detect_closed_jobs
    doesn't cover (anything not sourced from a re-pollable greenhouse/lever/
    ashby board). Rechecks the least-recently-checked jobs first (NULLs --
    never checked at all -- come before any real timestamp), so the whole
    backlog is gradually covered across successive runs rather than the
    same handful of jobs being rechecked over and over while the rest are
    never reached."""
    query = """
        SELECT * FROM jobs
        WHERE sheet_row_number IS NOT NULL
          AND (source_type IS NULL OR source_type NOT IN ('greenhouse', 'lever', 'ashby'))
        ORDER BY link_checked_at IS NOT NULL, link_checked_at ASC
    """
    if limit is not None:
        query += " LIMIT ?"
        jobs = conn.execute(query, (limit,)).fetchall()
    else:
        jobs = conn.execute(query).fetchall()

    checked = dead = 0
    for job in jobs:
        result = check_link_dead(job["url"], session=session)
        conn.execute("UPDATE jobs SET link_checked_at = CURRENT_TIMESTAMP WHERE id = ?", (job["id"],))
        conn.commit()
        checked += 1

        if result is not True:
            if workflow_run_id is not None:
                log_step(
                    conn, workflow_run_id=workflow_run_id, job_id=job["id"], step_name="link_check",
                    step_status="alive" if result is False else "inconclusive",
                )
            continue

        dead += 1
        company = None
        if job["company_id"] is not None:
            company = conn.execute("SELECT * FROM companies WHERE id = ?", (job["company_id"],)).fetchone()
        if main_ws is not None:
            remove_main_row(main_ws, job["id"])
        conn.execute(
            "UPDATE jobs SET status = 'closed', sheet_row_number = NULL WHERE id = ?", (job["id"],)
        )
        conn.commit()
        if job_log_ws is not None:
            upsert_job_log_row(job_log_ws, job, company, STAGE_CLOSED)
        if workflow_run_id is not None:
            log_step(
                conn, workflow_run_id=workflow_run_id, job_id=job["id"], step_name="link_check",
                step_status="dead",
            )

    return {"checked": checked, "dead": dead}
