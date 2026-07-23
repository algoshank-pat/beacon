"""Ingestion orchestration: Adzuna broad discovery + Greenhouse/Lever/Ashby targeted
tracking, dedup (exact URL + fuzzy cross-source), closed-job detection."""
from __future__ import annotations

import sqlite3

from app.cloud_platforms import resolve_cloud_platforms
from app.companies import get_or_create_company
from app.dedup import find_fuzzy_duplicate
from app.html_strip import strip_html
from app.http_client import RequestFailedError
from app.job_log import STAGE_CLOSED, upsert_job_log_row
from app.location_state import resolve_location_state
from app.observability import finish_workflow_run, log_step, start_workflow_run
from app.sheets import remove_main_row
from app.sources.adzuna import fetch_adzuna_jobs_for_keyword
from app.sources.ashby import fetch_ashby_jobs
from app.sources.greenhouse import fetch_greenhouse_jobs
from app.sources.lever import fetch_lever_jobs
from app.sources.smartrecruiters import fetch_smartrecruiters_jobs

SOURCE_FETCHERS = {
    "greenhouse": fetch_greenhouse_jobs,
    "lever": fetch_lever_jobs,
    "ashby": fetch_ashby_jobs,
    "smartrecruiters": fetch_smartrecruiters_jobs,
}


def upsert_job(conn: sqlite3.Connection, job: dict) -> tuple[int, str]:
    """Insert a normalized job dict. Returns (job_id, outcome), where outcome is
    'inserted', 'duplicate_url' (exact URL already present, skipped), or
    'duplicate_fuzzy' (new row inserted but flagged as a likely duplicate of an
    existing job for the same company)."""
    existing = conn.execute("SELECT id FROM jobs WHERE url = ?", (job["url"],)).fetchone()
    if existing is not None:
        return existing["id"], "duplicate_url"

    company_id = get_or_create_company(conn, job["company_name"], job["source_type"])
    description = strip_html(job.get("description_html"))
    duplicate_of = find_fuzzy_duplicate(conn, company_id, job["title"], job.get("location"))
    status = "duplicate" if duplicate_of else "new"

    cursor = conn.execute(
        """
        INSERT INTO jobs (
            company_id, title, url, apply_url, description, location,
            location_state, cloud_platforms, posted_at, salary_min, salary_max, salary_source,
            adzuna_salary_min, adzuna_salary_max, source_type,
            duplicate_of_job_id, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            company_id,
            job["title"],
            job["url"],
            job.get("apply_url"),
            description,
            job.get("location"),
            resolve_location_state(job.get("location")),
            resolve_cloud_platforms(f"{job['title']}\n{description}"),
            job.get("posted_at"),
            job.get("salary_min"),
            job.get("salary_max"),
            job.get("salary_source"),
            job.get("adzuna_salary_min"),
            job.get("adzuna_salary_max"),
            job["source_type"],
            duplicate_of,
            status,
        ),
    )
    conn.commit()
    return cursor.lastrowid, ("duplicate_fuzzy" if duplicate_of else "inserted")


def detect_closed_jobs(
    conn: sqlite3.Connection, company_id: int, source_type: str, current_urls: set,
    main_ws=None, job_log_ws=None,
) -> int:
    """Mark jobs from this company+source no longer present in the latest board
    fetch as closed. Only valid for sources that return the complete current-open
    set on every poll (greenhouse/lever/ashby) -- not Adzuna, which is a bounded
    keyword search, not a full-board listing (see app.link_check for Adzuna's
    own, different approach to the same problem).

    Real gap found and fixed: this used to only flip jobs.status to 'closed'
    in the DB and stop there -- nothing ever removed the row from Beacon, so
    a dead link just sat there indefinitely (confirmed live: 14 jobs already
    marked 'closed' were still showing on the live sheet). Now evicts to the
    Job Log the same way visa-scan/fit-scoring do, but only for a job that
    was actually on Beacon -- one still sitting at status='new' (never
    reached the Filter Engine) just needs the status flip so run_filter_engine
    naturally skips it, no Sheets I/O needed."""
    rows = conn.execute(
        """
        SELECT * FROM jobs
        WHERE company_id = ? AND source_type = ?
          AND status NOT IN ('closed', 'duplicate', 'approved', 'rejected')
        """,
        (company_id, source_type),
    ).fetchall()

    closed = 0
    for row in rows:
        if row["url"] in current_urls:
            continue

        was_on_beacon = row["sheet_row_number"] is not None
        conn.execute(
            "UPDATE jobs SET status = 'closed', sheet_row_number = NULL WHERE id = ?", (row["id"],)
        )
        conn.commit()
        closed += 1

        if not was_on_beacon:
            continue
        if main_ws is not None:
            remove_main_row(main_ws, row["id"])
        if job_log_ws is not None:
            company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
            upsert_job_log_row(job_log_ws, row, company, STAGE_CLOSED)

    return closed


def ingest_targeted_company(
    conn: sqlite3.Connection, company_row, *, workflow_run_id: int | None = None,
    main_ws=None, job_log_ws=None,
) -> dict:
    source_type = company_row["source_type"]
    fetcher = SOURCE_FETCHERS.get(source_type)
    if fetcher is None:
        return {"skipped": True, "reason": f"no poller for source_type={source_type!r}"}

    board_url = company_row["board_url"]
    if not board_url:
        return {"skipped": True, "reason": "no board_url configured"}

    try:
        if source_type == "smartrecruiters":
            # SmartRecruiters needs a per-posting detail call just to get a
            # description (see app.sources.smartrecruiters) -- known_urls
            # lets it skip that for postings already tracked, since only
            # genuinely new postings need fresh description text.
            known_urls = {
                row["url"]
                for row in conn.execute(
                    "SELECT url FROM jobs WHERE company_id = ? AND source_type = ?",
                    (company_row["id"], source_type),
                ).fetchall()
            }
            jobs = fetcher(board_url, known_urls=known_urls)
        else:
            jobs = fetcher(board_url)
    except RequestFailedError as exc:
        if workflow_run_id is not None:
            log_step(
                conn,
                workflow_run_id=workflow_run_id,
                step_name="ingest",
                step_status="failed",
                detail=f"{company_row['name']} ({source_type}): {exc}",
            )
        return {"error": str(exc)}

    inserted = duplicate_url = duplicate_fuzzy = 0
    for job in jobs:
        job["company_name"] = company_row["name"]
        if not job.get("url"):
            continue
        _, outcome = upsert_job(conn, job)
        if outcome == "inserted":
            inserted += 1
        elif outcome == "duplicate_url":
            duplicate_url += 1
        elif outcome == "duplicate_fuzzy":
            duplicate_fuzzy += 1

    current_urls = {job["url"] for job in jobs if job.get("url")}
    closed = detect_closed_jobs(
        conn, company_row["id"], source_type, current_urls, main_ws=main_ws, job_log_ws=job_log_ws,
    )

    if workflow_run_id is not None:
        log_step(
            conn,
            workflow_run_id=workflow_run_id,
            step_name="ingest",
            step_status="ok",
            detail=(
                f"{company_row['name']} ({source_type}): fetched={len(jobs)} inserted={inserted} "
                f"dup_url={duplicate_url} dup_fuzzy={duplicate_fuzzy} closed={closed}"
            ),
        )

    return {
        "fetched": len(jobs),
        "inserted": inserted,
        "duplicate_url": duplicate_url,
        "duplicate_fuzzy": duplicate_fuzzy,
        "closed": closed,
    }


def ingest_adzuna(
    conn: sqlite3.Connection,
    app_id: str,
    app_key: str,
    *,
    max_days_old: int = 30,
    workflow_run_id: int | None = None,
) -> dict:
    keywords = conn.execute(
        """
        SELECT keyword FROM filter_keywords
        WHERE is_active = 1 AND category IN ('role_keyword_include', 'tech_keyword_include')
        """
    ).fetchall()

    if not keywords:
        if workflow_run_id is not None:
            log_step(
                conn,
                workflow_run_id=workflow_run_id,
                step_name="ingest",
                step_status="skipped",
                detail="Adzuna skipped: zero active filter_keywords rows",
            )
        return {"skipped": True, "reason": "no active filter_keywords"}

    location_rows = conn.execute(
        "SELECT keyword FROM filter_keywords WHERE is_active = 1 AND category = 'location_include'"
    ).fetchall()
    location = location_rows[0]["keyword"] if location_rows else None

    inserted = duplicate_url = duplicate_fuzzy = failed = 0
    for row in keywords:
        keyword = row["keyword"]
        try:
            jobs = fetch_adzuna_jobs_for_keyword(
                app_id, app_key, keyword, location=location, max_days_old=max_days_old
            )
        except RequestFailedError as exc:
            failed += 1
            if workflow_run_id is not None:
                log_step(
                    conn,
                    workflow_run_id=workflow_run_id,
                    step_name="ingest",
                    step_status="failed",
                    detail=f"Adzuna keyword {keyword!r}: {exc}",
                )
            continue

        for job in jobs:
            if not job.get("url"):
                continue
            _, outcome = upsert_job(conn, job)
            if outcome == "inserted":
                inserted += 1
            elif outcome == "duplicate_url":
                duplicate_url += 1
            elif outcome == "duplicate_fuzzy":
                duplicate_fuzzy += 1

    if workflow_run_id is not None:
        log_step(
            conn,
            workflow_run_id=workflow_run_id,
            step_name="ingest",
            step_status="ok",
            detail=(
                f"Adzuna: {len(keywords)} keyword(s) queried, inserted={inserted} "
                f"dup_url={duplicate_url} dup_fuzzy={duplicate_fuzzy} failed_keywords={failed}"
            ),
        )

    return {
        "keywords_queried": len(keywords),
        "inserted": inserted,
        "duplicate_url": duplicate_url,
        "duplicate_fuzzy": duplicate_fuzzy,
        "failed_keywords": failed,
    }


def run_ingestion(conn: sqlite3.Connection, settings, main_ws=None, job_log_ws=None) -> dict:
    workflow_run_id = start_workflow_run(conn, "main_pipeline")

    if settings.adzuna_app_id and settings.adzuna_app_key:
        adzuna_result = ingest_adzuna(
            conn, settings.adzuna_app_id, settings.adzuna_app_key, workflow_run_id=workflow_run_id
        )
    else:
        adzuna_result = {"skipped": True, "reason": "Adzuna credentials not configured"}

    targeted_results = {}
    companies = conn.execute(
        "SELECT * FROM companies WHERE source_type IN ('greenhouse', 'lever', 'ashby', 'smartrecruiters')"
    ).fetchall()
    for company in companies:
        targeted_results[company["name"]] = ingest_targeted_company(
            conn, company, workflow_run_id=workflow_run_id, main_ws=main_ws, job_log_ws=job_log_ws,
        )

    total_inserted = adzuna_result.get("inserted", 0) + sum(
        r.get("inserted", 0) for r in targeted_results.values()
    )

    finish_workflow_run(conn, workflow_run_id, status="completed", jobs_ingested=total_inserted)

    return {
        "workflow_run_id": workflow_run_id,
        "adzuna": adzuna_result,
        "targeted": targeted_results,
        "total_inserted": total_inserted,
    }
