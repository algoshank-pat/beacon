"""Job Log Sheet writer — a SEPARATE spreadsheet from the main Beacon
tracking sheet, notifications deliberately left off. Where jobs go when
they don't (or stop) qualifying for Beacon: filtered out, visa-restricted,
scored below threshold, or the user set My Decision to "Reject".

One row per job, ever — upsert, not append. Mirrors Beacon's column set
(redesigned alongside it, same session) plus a "Reason for Rejection" field
explaining the exclusion, and its own Decision column (Accept/Reject) for
flagging a job to reconsider. Gated by filter_settings.job_log_enabled
(default true) -- callers pass job_log_ws=None to skip writes entirely (and
avoid the Sheets API calls) when disabled, rather than this module checking
the setting itself.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from gspread.utils import ValidationConditionType

from app.dates import format_central
from app.sheets import _format_lca_date, display_visa_flag, format_salary_range
from app.sheets_retry import call_with_retry

JOB_LOG_COLUMNS = [
    "Job ID", "Company", "Title", "Industry", "Location",
    # Right after "Location", same live-column-move as MAIN_SHEET_COLUMNS
    # in app.sheets -- see that constant's comment.
    "Location State",
    "Date Posted",
    "Reason for Rejection", "Salary Range", "Application Status", "Visa Flag",
    "My Decision", "Initial Fit Score", "Final Fit Score", "Employee Count",
    "Public/Private", "Funding Stage", "Revenue/Valuation", "Apply URL",
    "Decision", "Last Updated",
    # Appended at the end, same rationale as MAIN_SHEET_COLUMNS in app.sheets.
    "Cloud Platforms",
    # Same as app.sheets.MAIN_SHEET_COLUMNS -- real historical sponsorship
    # signal from app.lca_enrichment, distinct from Visa Flag.
    "DOL LCA Match", "Last Sponsored",
]

JOB_ID_COL_INDEX = JOB_LOG_COLUMNS.index("Job ID") + 1
SCORE_COL_INDEX = JOB_LOG_COLUMNS.index("Initial Fit Score") + 1
DECISION_COL_INDEX = JOB_LOG_COLUMNS.index("Decision") + 1

DECISION_VALUES = ["Accept", "Reject"]
VALIDATION_ROW_COUNT = 1000  # generous headroom for future growth

DECISION_ACCEPT_GREEN = {"red": 0.71, "green": 0.88, "blue": 0.71}
DECISION_REJECT_RED = {"red": 0.96, "green": 0.78, "blue": 0.78}
SCORE_HIGHLIGHT_BLUE = {"red": 0.80, "green": 0.88, "blue": 1.0}

STAGE_KEYWORD_MISMATCH = "Filtered Out - Title/Keyword Mismatch"
STAGE_SENIORITY = "Filtered Out - Seniority"
STAGE_LOCATION = "Filtered Out - Location"
STAGE_POSTED_DATE = "Filtered Out - Posted Date"
STAGE_COMPANY_CRITERIA = "Filtered Out - Company Criteria"
STAGE_VISA_RESTRICTED = "Visa Restricted"
STAGE_SCORED_BELOW_THRESHOLD = "Scored Below Threshold"
STAGE_USER_REJECTED = "Rejected (My Decision)"
STAGE_CLOSED = "Posting Closed"


def _col_letter(index: int) -> str:
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def get_job_log_worksheet(client, sheet_id: str):
    # See app.sheets.get_main_worksheet's identical fix -- same gap, same
    # repeated live 429/503 failures on this initial connection call.
    return call_with_retry(client.open_by_key, sheet_id).sheet1


def resolve_job_log_worksheet(settings, filter_settings: dict):
    """Returns a ready-to-write Job Log worksheet, or None if job_log_enabled
    is off or the Job Log Sheet isn't configured -- callers skip all Job Log
    writes (and the Sheets API calls that would go with them) when this
    returns None."""
    if not filter_settings.get("job_log_enabled", True):
        return None
    if not settings.google_sheets_credentials_path or not settings.google_job_log_sheet_id:
        return None

    import gspread

    client = gspread.service_account(filename=settings.google_sheets_credentials_path)
    ws = get_job_log_worksheet(client, settings.google_job_log_sheet_id)
    ensure_job_log_headers(ws)
    return ws


def ensure_job_log_headers(ws) -> bool:
    """Idempotent: only writes/formats if the header row isn't already set up.
    Returns True if it just set things up, False if already correct. Like
    app.sheets.ensure_main_sheet_headers, this only handles "already correct"
    and "truly empty" -- deployed via a full rebuild, not an incremental
    in-place migration."""
    existing = ws.row_values(1)
    if existing == JOB_LOG_COLUMNS:
        return False
    ws.update(values=[JOB_LOG_COLUMNS], range_name="A1")

    decision_col = _col_letter(DECISION_COL_INDEX)
    ws.add_validation(
        f"{decision_col}2:{decision_col}{VALIDATION_ROW_COUNT}",
        ValidationConditionType.one_of_list,
        DECISION_VALUES,
        showCustomUi=True,
    )

    sheet_id = ws.id

    # Conditional-format rules are NOT naturally overwriting like
    # add_validation above -- any already on the sheet must be deleted first
    # (highest index first, so deleting doesn't shift the next index out from
    # under itself) or they stack as duplicates on every repeat call where
    # the header genuinely changes. Missing this is what let rules pile up
    # (12 instead of 3) across this session's two Location State column
    # changes before being caught and fixed.
    existing_rule_count = 0
    for sheet in ws.spreadsheet.fetch_sheet_metadata()["sheets"]:
        if sheet["properties"]["sheetId"] == sheet_id:
            existing_rule_count = len(sheet.get("conditionalFormats", []))
            break

    full_row_range = {
        "sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": 0,
        "endColumnIndex": len(JOB_LOG_COLUMNS),
    }
    score_col_range = {
        "sheetId": sheet_id, "startRowIndex": 1,
        "startColumnIndex": SCORE_COL_INDEX - 1, "endColumnIndex": SCORE_COL_INDEX,
    }
    requests = [
        {"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": i}}
        for i in range(existing_rule_count - 1, -1, -1)
    ]
    requests += [
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [full_row_range],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": f'=${decision_col}2="Accept"'}],
                        },
                        "format": {"backgroundColor": DECISION_ACCEPT_GREEN},
                    },
                },
                "index": 0,
            }
        },
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [full_row_range],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": f'=${decision_col}2="Reject"'}],
                        },
                        "format": {"backgroundColor": DECISION_REJECT_RED},
                    },
                },
                "index": 1,
            }
        },
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [score_col_range],
                    "booleanRule": {
                        "condition": {"type": "NOT_BLANK"},
                        "format": {"backgroundColor": SCORE_HIGHLIGHT_BLUE},
                    },
                },
                "index": 2,
            }
        },
    ]
    ws.spreadsheet.batch_update({"requests": requests})
    return True


def find_existing_row(ws, job_id: int) -> int | None:
    values = call_with_retry(ws.col_values, JOB_ID_COL_INDEX)
    target = str(job_id)
    for i, value in enumerate(values[1:], start=2):  # skip header row
        if str(value) == target:
            return i
    return None


def _build_row(
    job: sqlite3.Row,
    company: sqlite3.Row | None,
    reason: str,
    fit_score: int | None,
    my_decision: str | None,
    salary_min: int | None,
    salary_max: int | None,
) -> list:
    row = {col: "" for col in JOB_LOG_COLUMNS}
    row["Job ID"] = job["id"]
    row["Company"] = company["name"] if company is not None else ""
    row["Title"] = job["title"] or ""
    row["Industry"] = (company["industry"] if company is not None else None) or ""
    row["Location"] = job["location"] or ""
    row["Location State"] = job["location_state"] or ""
    row["Cloud Platforms"] = job["cloud_platforms"] or ""
    row["Date Posted"] = format_central(job["posted_at"])
    row["Reason for Rejection"] = reason
    row["Salary Range"] = format_salary_range(
        salary_min, salary_max, job["adzuna_salary_min"], job["adzuna_salary_max"]
    )
    row["Visa Flag"] = display_visa_flag(job["visa_flag"])
    row["My Decision"] = my_decision or ""
    row["Initial Fit Score"] = fit_score if fit_score is not None else ""
    if company is not None:
        row["Employee Count"] = company["employee_count"] or company["employee_count_range"] or ""
        row["Public/Private"] = company["company_type"] or ""
        row["Funding Stage"] = company["funding_stage"] or ""
        row["Revenue/Valuation"] = company["revenue_or_valuation"] or ""
        row["DOL LCA Match"] = company["dol_lca_employer_name"] or ""
        row["Last Sponsored"] = _format_lca_date(company["last_lca_certified_date"])
    row["Apply URL"] = job["apply_url"] or job["url"]
    row["Last Updated"] = format_central(datetime.now(timezone.utc))
    return [row[col] for col in JOB_LOG_COLUMNS]


def upsert_job_log_row(
    ws,
    job: sqlite3.Row,
    company: sqlite3.Row | None,
    reason: str,
    fit_score: int | None = None,
    my_decision: str | None = None,
) -> None:
    """One row per job, ever. Creates it on first sight (e.g. filtered out,
    or evicted from Beacon); a job landing here again (shouldn't normally
    happen -- exclusion is terminal) updates that same row in place rather
    than adding a new one. Never touches this sheet's own Decision column --
    that's the user's, set directly in the Sheet, and must survive every
    automated update.

    `job`/`company` are passed as full DB rows (not scattered fields) so
    this can populate the full mirrored column set the same way
    app.sheets.build_main_row does for Beacon -- salary_min/salary_max are
    still passed explicitly rather than read from `job` since the caller
    may have a fresher value than what's already committed (see
    app.sheets.add_job_to_beacon for why)."""
    existing_row = find_existing_row(ws, job["id"])
    row_values = _build_row(
        job, company, reason, fit_score, my_decision,
        salary_min=job["salary_min"], salary_max=job["salary_max"],
    )

    if existing_row is None:
        # table_range MUST be bounded -- see app.sheets.add_job_to_beacon's
        # identical fix for the live incident this guards against (an
        # unbounded append_row silently balloons gridProperties.columnCount
        # on nearly every call until the workbook hits its 10M-cell ceiling).
        last_col = _col_letter(len(JOB_LOG_COLUMNS))
        call_with_retry(
            ws.append_row, row_values, value_input_option="USER_ENTERED",
            table_range=f"A1:{last_col}1",
        )
        return

    # Every column except this sheet's own Decision (which the user sets
    # directly and must survive automated updates) -- write everything up
    # to Decision's index, then everything after it, skipping over it.
    decision_idx = JOB_LOG_COLUMNS.index("Decision")
    before = row_values[:decision_idx]
    after = row_values[decision_idx + 1:]
    if before:
        call_with_retry(
            ws.update,
            values=[before],
            range_name=f"A{existing_row}:{_col_letter(decision_idx)}{existing_row}",
            value_input_option="USER_ENTERED",
        )
    if after:
        call_with_retry(
            ws.update,
            values=[after],
            range_name=f"{_col_letter(decision_idx + 2)}{existing_row}:{_col_letter(len(JOB_LOG_COLUMNS))}{existing_row}",
            value_input_option="USER_ENTERED",
        )
