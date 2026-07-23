"""One-time script: rebuilds the Beacon and Job Log Sheets under the
redesigned 20/19-column schema (app.sheets.MAIN_SHEET_COLUMNS /
app.job_log.JOB_LOG_COLUMNS).

Source of truth for each sheet:
- Beacon: whichever Job IDs are CURRENTLY present on the live Beacon sheet
  right now (read before clearing) -- more reliable than jobs.sheet_row_number,
  which has known drift for a handful of rows (jobs whose DB status moved on
  after their row was added, e.g. re-filtered after a later rule change).
  Repopulated from current DB state: company enrichment, real/Adzuna salary,
  visa flag, and the latest fit score if one exists (My Decision is set to
  "AI Scored" for those, "New" otherwise).
- Job Log: EVERY excluded job in the database (status='filtered_out', or
  status='scored' with no current Beacon row), not just the ones already on
  the live Job Log sheet -- job_log_enabled was off for a stretch earlier in
  this project, so the live sheet is missing ~585 of 767 real exclusions.
  Visa-restricted reasons are reconstructed from jobs.visa_snippet to match
  today's "Visa Restricted: <snippet>" format exactly.

Old conditional-formatting rules and data-validation rules are stripped from
each sheet before the new headers are written (Worksheet.clear() only clears
cell values, not formatting/validation) -- otherwise ensure_*_headers would
layer new rules on top of the old ones instead of replacing them.

Dry-run by default (prints counts, writes nothing). Pass --confirm to
actually clear and repopulate the live sheets.

Run: python scripts/rebuild_sheets.py [--confirm]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.job_log import (  # noqa: E402
    STAGE_SCORED_BELOW_THRESHOLD,
    STAGE_VISA_RESTRICTED,
    _build_row as build_job_log_row,
    ensure_job_log_headers,
    get_job_log_worksheet,
)
from app.sheets import (  # noqa: E402
    MY_DECISION_AI_SCORED,
    MY_DECISION_COL_INDEX,
    build_main_row,
    ensure_main_sheet_headers,
    get_client,
    get_main_worksheet,
)

CHUNK_SIZE = 500


def _strip_old_formatting(ws) -> None:
    """Removes every existing conditional-format rule and data-validation
    rule from the sheet. Worksheet.clear() only clears cell values, so
    without this, ensure_*_headers's fresh rules would stack on top of
    whatever the old schema already had (wrong column refs, duplicated
    Approve/Deny highlighting, etc.)."""
    meta = ws.spreadsheet.fetch_sheet_metadata(
        {"fields": "sheets.properties.sheetId,sheets.conditionalFormats"}
    )
    sheet_meta = next(s for s in meta["sheets"] if s["properties"]["sheetId"] == ws.id)
    rule_count = len(sheet_meta.get("conditionalFormats", []))

    requests = [
        {"deleteConditionalFormatRule": {"sheetId": ws.id, "index": i}}
        for i in range(rule_count - 1, -1, -1)  # delete highest index first
    ]
    requests.append({"setDataValidation": {"range": {"sheetId": ws.id}}})  # clears all validation
    if requests:
        ws.spreadsheet.batch_update({"requests": requests})


def _chunked(rows: list, size: int):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def build_beacon_rows(conn, job_ids: list[int]) -> list[list]:
    rows = []
    for job_id in job_ids:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            print(f"  [skip] job {job_id} on the live sheet but missing from the DB")
            continue
        company = None
        if job["company_id"] is not None:
            company = conn.execute(
                "SELECT * FROM companies WHERE id = ?", (job["company_id"],)
            ).fetchone()
        score_row = conn.execute(
            "SELECT score FROM fit_scores WHERE job_id = ? ORDER BY scored_at DESC LIMIT 1", (job_id,)
        ).fetchone()
        score = score_row["score"] if score_row else None

        row = build_main_row(job, company, score=score, salary_min=job["salary_min"], salary_max=job["salary_max"])
        if score is not None:
            row[MY_DECISION_COL_INDEX - 1] = MY_DECISION_AI_SCORED
        rows.append(row)
    return rows


def build_job_log_rows(conn) -> list[list]:
    excluded = conn.execute(
        "SELECT * FROM jobs WHERE status = 'filtered_out' "
        "OR (status = 'scored' AND sheet_row_number IS NULL)"
    ).fetchall()

    rows = []
    for job in excluded:
        company = None
        if job["company_id"] is not None:
            company = conn.execute(
                "SELECT * FROM companies WHERE id = ?", (job["company_id"],)
            ).fetchone()

        fit_score = None
        my_decision = None
        if job["status"] == "filtered_out" and job["rejection_reason"] == "Visa Restricted":
            reason = (
                f"{STAGE_VISA_RESTRICTED}: {job['visa_snippet']}" if job["visa_snippet"] else STAGE_VISA_RESTRICTED
            )
        elif job["status"] == "scored":
            reason = STAGE_SCORED_BELOW_THRESHOLD
            score_row = conn.execute(
                "SELECT score FROM fit_scores WHERE job_id = ? ORDER BY scored_at DESC LIMIT 1", (job["id"],)
            ).fetchone()
            fit_score = score_row["score"] if score_row else None
            my_decision = MY_DECISION_AI_SCORED
        else:
            reason = job["rejection_reason"] or "Filtered Out"

        rows.append(
            build_job_log_row(
                job, company, reason, fit_score, my_decision,
                salary_min=job["salary_min"], salary_max=job["salary_max"],
            )
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true", help="Actually clear and repopulate the live sheets.")
    args = parser.parse_args()

    settings = get_settings()
    client = get_client(settings.google_sheets_credentials_path)
    main_ws = get_main_worksheet(client, settings.google_sheet_id)
    job_log_ws = get_job_log_worksheet(client, settings.google_job_log_sheet_id)

    beacon_job_ids = []
    seen = set()
    for value in main_ws.col_values(1)[1:]:
        if not value.strip():
            continue
        try:
            job_id = int(value)
        except ValueError:
            continue
        if job_id not in seen:
            seen.add(job_id)
            beacon_job_ids.append(job_id)

    conn = get_connection()
    try:
        beacon_rows = build_beacon_rows(conn, beacon_job_ids)
        job_log_rows = build_job_log_rows(conn)
    finally:
        conn.close()

    print(f"Beacon: {len(beacon_job_ids)} job IDs on the live sheet -> {len(beacon_rows)} rows to write")
    print(f"Job Log: {len(job_log_rows)} excluded jobs in the DB -> {len(job_log_rows)} rows to write")

    if not args.confirm:
        print("\nDry run only -- pass --confirm to clear and repopulate the live sheets.")
        return

    print("\nRebuilding Beacon...")
    main_ws.clear()
    _strip_old_formatting(main_ws)
    ensure_main_sheet_headers(main_ws)
    for chunk in _chunked(beacon_rows, CHUNK_SIZE):
        main_ws.append_rows(chunk, value_input_option="USER_ENTERED")
    print(f"  wrote {len(beacon_rows)} rows")

    print("Rebuilding Job Log...")
    job_log_ws.clear()
    _strip_old_formatting(job_log_ws)
    ensure_job_log_headers(job_log_ws)
    for chunk in _chunked(job_log_rows, CHUNK_SIZE):
        job_log_ws.append_rows(chunk, value_input_option="USER_ENTERED")
    print(f"  wrote {len(job_log_rows)} rows")

    print("\nDone.")


if __name__ == "__main__":
    main()
