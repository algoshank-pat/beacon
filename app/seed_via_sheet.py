"""Seed-via-Sheet — lets the user onboard a new company just by typing its
name into Beacon, without knowing anything about Greenhouse/Lever/Ashby/
SmartRecruiters.

Trigger: a Beacon row with Job ID = "SEED" and Company = a name the user
typed in manually (every other column left blank). Runs as the first step
of the scheduled pipeline, before app.ingest, so a newly-onboarded
company's postings start flowing in on the SAME run rather than waiting for
the next one.

For each such row, not yet processed (Title still blank):
- Guess a handful of plausible board slugs from the company name (lowercase,
  strip punctuation and common corporate suffixes, hyphenated/no-hyphen
  variants) and probe Greenhouse, then Lever, then Ashby, then
  SmartRecruiters with each one.
- A candidate is accepted only if the API call succeeds, returns at least
  one job, AND the guessed slug appears in that job's own URL -- guards
  against a stale/misleading response for a slug that doesn't actually
  belong to this company (ATS redirects, generic placeholder boards).
- On a match: creates (or reuses, by normalized-name match, same rule as
  app.companies.get_or_create_company) a companies row with source_type/
  board_url set and priority_tier="A" -- same tier as the other manually-
  curated batches -- and writes a one-line outcome into the Title cell.
- On no match: writes a one-line failure message into Title instead.

Cleanup happens the FOLLOWING run: any Job ID="SEED" row whose Title is
already non-blank (i.e., its outcome was already written and the user has
had a chance to see it) gets deleted outright -- it was never a real job
posting, just a one-time onboarding request.
"""
from __future__ import annotations

import re
import sqlite3

import requests

from app.companies import _normalize
from app.http_client import RequestFailedError
from app.observability import log_step
from app.sheets import MAIN_SHEET_COLUMNS
from app.sheets_retry import call_with_retry
from app.sources.ashby import fetch_ashby_jobs
from app.sources.greenhouse import fetch_greenhouse_jobs
from app.sources.lever import fetch_lever_jobs
from app.sources.smartrecruiters import fetch_smartrecruiters_jobs

SEED_MARKER = "SEED"

JOB_ID_COL_INDEX = MAIN_SHEET_COLUMNS.index("Job ID") + 1
COMPANY_COL_INDEX = MAIN_SHEET_COLUMNS.index("Company") + 1
TITLE_COL_INDEX = MAIN_SHEET_COLUMNS.index("Title") + 1

# Failure modes expected from routine slug-guessing (most candidates won't
# exist): 404s (requests.HTTPError via raise_for_status), connection/DNS
# failures, and a handful of retryable-exhausted cases (RequestFailedError).
# Not a bare `except Exception` -- these are the specific, enumerable ways a
# wrong guess fails.
_PROBE_EXCEPTIONS = (RequestFailedError, requests.RequestException, ValueError)

_CORPORATE_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company)\b\.?", re.IGNORECASE
)


def _col_letter(index: int) -> str:
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _slug_candidates(company_name: str) -> list[str]:
    """A handful of plausible board-slug guesses, most-likely-first: with and
    without common corporate suffixes ("Inc", "LLC", ...), squashed together
    or hyphenated."""
    stripped = _CORPORATE_SUFFIXES.sub("", company_name).strip()
    candidates: list[str] = []
    for source in (stripped, company_name):
        no_punct = re.sub(r"[^a-zA-Z0-9\s-]", "", source).strip()
        if not no_punct:
            continue
        squashed = re.sub(r"\s+", "", no_punct).lower()
        hyphenated = re.sub(r"\s+", "-", no_punct).lower()
        for candidate in (squashed, hyphenated):
            if candidate and candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _verify_match(slug: str, jobs: list[dict]) -> bool:
    if not jobs:
        return False
    needle = slug.lower()
    return any(needle in (job.get("url") or "").lower() for job in jobs)


def probe_boards(company_name: str) -> dict | None:
    """Tries each ATS with each slug candidate (Greenhouse, then Lever, then
    Ashby -- same order as app.ingest.SOURCE_FETCHERS). Returns
    {"source_type", "board_url", "job_count"} on the first verified match, or
    None if nothing matched."""
    probes = [
        ("greenhouse", fetch_greenhouse_jobs),
        ("lever", fetch_lever_jobs),
        ("ashby", fetch_ashby_jobs),
        ("smartrecruiters", fetch_smartrecruiters_jobs),
    ]
    for slug in _slug_candidates(company_name):
        for source_type, fetcher in probes:
            try:
                jobs = fetcher(slug)
            except _PROBE_EXCEPTIONS:
                continue
            if _verify_match(slug, jobs):
                return {"source_type": source_type, "board_url": slug, "job_count": len(jobs)}
    return None


def _find_or_create_company(conn: sqlite3.Connection, name: str, source_type: str, board_url: str) -> int:
    normalized = _normalize(name)
    for row in conn.execute("SELECT id, name FROM companies").fetchall():
        if _normalize(row["name"]) == normalized:
            conn.execute(
                "UPDATE companies SET source_type = ?, board_url = ?, "
                "priority_tier = COALESCE(priority_tier, 'A') WHERE id = ?",
                (source_type, board_url, row["id"]),
            )
            conn.commit()
            return row["id"]

    cursor = conn.execute(
        "INSERT INTO companies (name, source_type, board_url, priority_tier) VALUES (?, ?, ?, 'A')",
        (name.strip(), source_type, board_url),
    )
    conn.commit()
    return cursor.lastrowid


def _seed_rows(main_ws) -> list[tuple[int, str, str]]:
    """Returns (1-based sheet row number, company name, existing Title) for
    every row whose Job ID cell is exactly "SEED"."""
    job_ids = call_with_retry(main_ws.col_values, JOB_ID_COL_INDEX)
    companies = call_with_retry(main_ws.col_values, COMPANY_COL_INDEX)
    titles = call_with_retry(main_ws.col_values, TITLE_COL_INDEX)

    rows = []
    for idx in range(1, len(job_ids)):  # skip header row
        if job_ids[idx].strip().upper() != SEED_MARKER:
            continue
        company_name = companies[idx].strip() if idx < len(companies) else ""
        title = titles[idx].strip() if idx < len(titles) else ""
        rows.append((idx + 1, company_name, title))
    return rows


def run_seed_via_sheet(conn: sqlite3.Connection, main_ws, workflow_run_id: int | None = None) -> dict:
    """Onboards companies flagged via Job ID="SEED" rows and cleans up rows
    already processed on a prior run. No-op (all-zero result) if main_ws
    isn't configured, so this never touches the DB without a Sheet to read
    from."""
    empty_result = {"processed": 0, "added": 0, "not_found": 0, "cleaned_up": 0}
    if main_ws is None:
        return empty_result

    # Bottom-to-top so a deletion doesn't shift the row numbers of rows
    # still to be handled below it in this same pass.
    rows = sorted(_seed_rows(main_ws), key=lambda r: r[0], reverse=True)

    processed = added = not_found = cleaned_up = 0
    for row_number, company_name, existing_title in rows:
        if existing_title:
            # Already processed on a prior run -- the user has had a chance
            # to see the outcome. This row never was a real job posting.
            call_with_retry(main_ws.delete_rows, row_number)
            cleaned_up += 1
            continue

        if not company_name:
            continue

        match = probe_boards(company_name)
        if match is None:
            outcome = "No Greenhouse/Lever/Ashby/SmartRecruiters board found -- add board_url manually if you know it"
            not_found += 1
        else:
            _find_or_create_company(conn, company_name, match["source_type"], match["board_url"])
            outcome = f"Added via {match['source_type'].title()} ({match['job_count']} open role(s))"
            added += 1
        processed += 1

        col = _col_letter(TITLE_COL_INDEX)
        call_with_retry(
            main_ws.update, values=[[outcome]], range_name=f"{col}{row_number}",
            value_input_option="USER_ENTERED",
        )

        if workflow_run_id is not None:
            log_step(
                conn, workflow_run_id=workflow_run_id, step_name="seed_via_sheet",
                step_status="ok" if match else "not_found", detail=f"{company_name}: {outcome}",
            )

    return {"processed": processed, "added": added, "not_found": not_found, "cleaned_up": cleaned_up}
