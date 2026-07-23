"""Salary Refresh — for Beacon jobs still missing a real salary after the
cheap description-only extraction `app.sheets.add_job_to_beacon` does at
add-time (deliberately no page-fetch fallback there, to stay fast for every
passing job -- see its own docstring). This periodically re-attempts
extraction using the full posting page instead of just the stored (often
Adzuna-truncated-to-500-chars) description.

Real gap found live, reported directly: a job's real salary line often sits
well past Adzuna's 500-char description cap -- "A reasonable estimate of
the base salary range for this level is $85,000 to $95,000..." was
confirmed to exist in a real posting's full text while Beacon showed
Adzuna's unrelated algorithmic estimate instead, since the description
truncation cut the description off before that sentence.
`app.salary_extraction.resolve_posted_salary()` already existed for exactly
this (built, tested) but was never called from anywhere -- this module is
that missing wiring.

Not every job's full page is actually fetchable this way: confirmed live,
some Adzuna `redirect_url`s resolve server-side to a page with real JD text
embedded (fetchable with a plain HTTP client), while others are a
client-side JavaScript redirect to an external board (e.g. ZipRecruiter) --
NOT fetchable without executing that JS, which `requests` can't do. A job
whose page can't be fetched, or whose fetched page still has no parseable
salary line, is simply left with whatever it already had (real range if
found earlier, else Adzuna's estimate); `salary_checked_at` is stamped
either way so it isn't re-fetched forever."""
from __future__ import annotations

import sqlite3

from app.salary_extraction import resolve_posted_salary
from app.sheets import update_salary_range


def run_salary_refresh(
    conn: sqlite3.Connection, limit: int | None = None, main_ws=None,
) -> dict:
    """Re-attempts salary extraction (via the full posting page) for jobs
    currently on Beacon that never found a real salary at add-time. No
    external API quota to respect here (same reasoning as
    app.link_check's batch_size) -- `limit` exists purely to bound how much
    latency one pipeline run takes on, not to protect a rate limit."""
    query = """
        SELECT * FROM jobs
        WHERE sheet_row_number IS NOT NULL
          AND salary_min IS NULL
          AND salary_checked_at IS NULL
        ORDER BY id
    """
    if limit is not None:
        query += " LIMIT ?"
        jobs = conn.execute(query, (limit,)).fetchall()
    else:
        jobs = conn.execute(query).fetchall()

    checked = found = 0
    for job in jobs:
        salary_min, salary_max = resolve_posted_salary(job["description"], job["url"])
        conn.execute(
            "UPDATE jobs SET salary_checked_at = CURRENT_TIMESTAMP, "
            "salary_min = COALESCE(?, salary_min), salary_max = COALESCE(?, salary_max), "
            "salary_source = COALESCE(?, salary_source) WHERE id = ?",
            (salary_min, salary_max, "job posting page" if salary_min is not None else None, job["id"]),
        )
        conn.commit()
        checked += 1

        if salary_min is not None:
            found += 1
            if main_ws is not None:
                update_salary_range(
                    main_ws, job["id"], salary_min, salary_max,
                    job["adzuna_salary_min"], job["adzuna_salary_max"],
                )

    return {"checked": checked, "found": found}
