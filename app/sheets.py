"""Google Sheets integration — the Beacon sheet. This IS the notification
mechanism: every automated write triggers Google's own native "Any changes
are made" notification rule (enabled once, manually, in the Sheets UI)
since the service account is a distinct identity from the user. No app-side
notification/email code.

Beacon is the live candidate list: a job gets a row the moment it clears
the Filter Engine, with score and visa flag blank until visa-scan/fit-score
(both separately scheduled) fill them in. If a job later turns out
visa-restricted, scores below threshold, or the user sets My Decision to
"Reject", its row is REMOVED from here and moved to the Job Log ("Filtered")
sheet instead -- Beacon stays a clean "still live" list, not gated behind
waiting for a score to compute. See app.job_log for the eviction destination.

Column set redesigned (rebuilt, not migrated in place) per direct request:
Salary Min/Max/Adzuna-Estimated-Min/Max consolidated into one "Salary Range"
column (real range, or Adzuna's estimate suffixed " (est.)" and pink-
highlighted as a fallback signal); Visa Flag values relabeled for display
(the internal restricted/sponsors/unclear/no_mention/pending values used
throughout app.visa_scan are translated to Sponsored/No sponsor/Unclear/
No mention/Visa Check Pending only here, at write time); a new "My Decision"
column (New/Go Score/AI Score Pending/AI Scored/Manual Scored/Reject)
replaces the earlier "Score Request" column with a fuller workflow, kept
fully independent of the existing Decision (Pending/Approve/Deny) approval
gate; a new fully-manual "Application Status" column tracks post-application
interview stages. ~12 previously-empty columns were dropped (confirmed empty
on the live sheet before doing so): Fit Score (Post-Resume), Handoff Prompt,
Resume/Cover Letter File Path, Found Via, Bonus & Other Comp, Recruiter
Name/Email, Interview Notes, Date Notified, Date Applied, LinkedIn
Connections Link. Rejection Reason and Reminder Sent At were both initially
slated for removal too but kept -- the first is load-bearing for the
Approval Poller's Deny flow, the second is what makes the stalled-reminder
notification fire at all (writing to a Sheet cell is this app's entire
notification mechanism; moving that tracking to the DB alone would make
reminders go silent).
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import gspread
from gspread.utils import ValidationConditionType

from app.dates import CENTRAL, format_central, parse_datetime
from app.observability import log_step
from app.salary_extraction import extract_salary_range
from app.sheets_retry import call_with_retry

MAIN_SHEET_COLUMNS = [
    "Job ID", "Company", "Title", "Industry", "Location",
    # Right after "Location" per direct request. Originally appended at the
    # end to avoid a reorder (see git history/RUNBOOK for that reasoning);
    # moved here via a live column insert (app.sheets shifts every
    # existing cell right of the insertion point, not just a Python list
    # reorder) once the user asked for it explicitly. Computed by
    # app.location_state at ingest time from the free-text Location column.
    "Location State",
    "Date Posted",
    "Salary Range", "Visa Flag", "Decision", "Rejection Reason",
    "Reminder Sent At", "My Decision", "Initial Fit Score", "Final Fit Score",
    "Employee Count", "Public/Private", "Funding Stage", "Revenue/Valuation",
    "Apply URL", "Application Status",
    # Appended at the end -- see "Location State" above for why a brand-new
    # column goes here rather than somewhere visually nicer: this schema's
    # column writes are positional, and appending needs no live data move.
    # Computed by app.cloud_platforms at ingest time from title+description.
    "Cloud Platforms",
    # Real historical sponsorship signal from app.lca_enrichment -- distinct
    # from Visa Flag, which only ever reads this one posting's own text.
    # "DOL LCA Match" shows the exact employer name matched in DOL's public
    # LCA disclosure data (blank if never matched), so a wrong match is
    # visible and auditable rather than a silent black box. "Last Sponsored"
    # is that employer's most recent Certified/Certified-Withdrawn LCA date.
    "DOL LCA Match", "Last Sponsored",
]

JOB_ID_COL_INDEX = MAIN_SHEET_COLUMNS.index("Job ID") + 1
DATE_POSTED_COL_INDEX = MAIN_SHEET_COLUMNS.index("Date Posted") + 1
INDUSTRY_COL_INDEX = MAIN_SHEET_COLUMNS.index("Industry") + 1
SALARY_RANGE_COL_INDEX = MAIN_SHEET_COLUMNS.index("Salary Range") + 1
VISA_FLAG_COL_INDEX = MAIN_SHEET_COLUMNS.index("Visa Flag") + 1
CLOUD_PLATFORMS_COL_INDEX = MAIN_SHEET_COLUMNS.index("Cloud Platforms") + 1
DECISION_COL_INDEX = MAIN_SHEET_COLUMNS.index("Decision") + 1
REJECTION_REASON_COL_INDEX = MAIN_SHEET_COLUMNS.index("Rejection Reason") + 1
REMINDER_SENT_AT_COL_INDEX = MAIN_SHEET_COLUMNS.index("Reminder Sent At") + 1
MY_DECISION_COL_INDEX = MAIN_SHEET_COLUMNS.index("My Decision") + 1
SCORE_COL_INDEX = MAIN_SHEET_COLUMNS.index("Initial Fit Score") + 1
EMPLOYEE_COUNT_COL_INDEX = MAIN_SHEET_COLUMNS.index("Employee Count") + 1
REVENUE_VALUATION_COL_INDEX = MAIN_SHEET_COLUMNS.index("Revenue/Valuation") + 1
DOL_LCA_MATCH_COL_INDEX = MAIN_SHEET_COLUMNS.index("DOL LCA Match") + 1
LAST_SPONSORED_COL_INDEX = MAIN_SHEET_COLUMNS.index("Last Sponsored") + 1

DECISION_VALUES = ["Pending", "Approve", "Deny"]

# My Decision state machine -- New (app default) -> Go Score (user requests
# scoring) -> AI Score Pending (app, claimed for a fit-scoring run, stays
# here if that run's API call fails so it's retried automatically next time)
# -> AI Scored (app, done) / Manual Scored (user, independent of the AI path)
# / Reject (user, evicts the row to the Job Log). See app.fit_scoring.
MY_DECISION_NEW = "New"
MY_DECISION_GO_SCORE = "Go Score"
MY_DECISION_AI_SCORE_PENDING = "AI Score Pending"
MY_DECISION_AI_SCORED = "AI Scored"
MY_DECISION_MANUAL_SCORED = "Manual Scored"
MY_DECISION_REJECT = "Reject"
MY_DECISION_VALUES = [
    MY_DECISION_NEW, MY_DECISION_GO_SCORE, MY_DECISION_AI_SCORE_PENDING,
    MY_DECISION_AI_SCORED, MY_DECISION_MANUAL_SCORED, MY_DECISION_REJECT,
]

APPLICATION_STATUS_VALUES = ["Applied", "Tech Rounds", "Talent Acquisition", "Panel", "Rejected"]

# Translates app.visa_scan's internal jobs.visa_flag values to the Sheet's
# user-facing labels -- applied only here, at write time, so the DB/pipeline
# logic never has to know about display strings.
VISA_FLAG_LABELS = {
    "restricted": "No sponsor",
    "sponsors": "Sponsored",
    "unclear": "Unclear",
    "no_mention": "No mention",
    "pending": "Visa Check Pending",
}


def display_visa_flag(internal_value: str | None) -> str:
    if not internal_value:
        return ""
    return VISA_FLAG_LABELS.get(internal_value, internal_value)


def format_salary_range(
    salary_min: int | None, salary_max: int | None,
    adzuna_salary_min: int | None, adzuna_salary_max: int | None,
) -> str:
    """Real posted range wins if present; falls back to Adzuna's algorithmic
    estimate, suffixed " (est.)" -- that suffix is also what the Sheet's
    conditional-format rule keys off of to apply the pink fallback highlight,
    so the two must stay in sync if this format ever changes."""
    if salary_min is not None and salary_max is not None:
        return f"${salary_min:,} - ${salary_max:,}"
    if adzuna_salary_min is not None and adzuna_salary_max is not None:
        return f"${adzuna_salary_min:,} - ${adzuna_salary_max:,} (est.)"
    return ""


DECISION_HEADER_GREEN = {"red": 0.71, "green": 0.88, "blue": 0.71}
DECISION_APPROVE_GREEN = {"red": 0.71, "green": 0.88, "blue": 0.71}
DECISION_DENY_RED = {"red": 0.96, "green": 0.78, "blue": 0.78}
SCORE_HIGHLIGHT_BLUE = {"red": 0.80, "green": 0.88, "blue": 1.0}
INDUSTRY_DEPRIORITIZE_GREY = {"red": 0.85, "green": 0.85, "blue": 0.85}
SALARY_ESTIMATE_PINK = {"red": 0.98, "green": 0.85, "blue": 0.90}
# Deliberately distinct from DECISION_APPROVE_GREEN/SCORE_HIGHLIGHT_BLUE
# above so a "posted today"/"posted this week" row isn't visually confused
# with an approved row or a scored cell at a glance.
POSTED_TODAY_BLUE = {"red": 0.68, "green": 0.85, "blue": 0.92}
POSTED_THIS_WEEK_GREEN = {"red": 0.82, "green": 0.94, "blue": 0.75}
VALIDATION_ROW_COUNT = 3_000  # same fixed-range gap as CONDITIONAL_FORMAT_ROW_HEADROOM
# below -- was 1000, already exceeded by live data (2551+ rows) before this
# fix, meaning Decision/My Decision/Application Status dropdowns had
# already silently stopped applying to newer rows.
# Real gap found while adding the two rules above: every existing
# conditional-format rule's range omits endRowIndex, which the Sheets API
# resolves to "the sheet's current row count at rule-creation time" --  NOT
# a dynamically-growing range. Confirmed live: the 5 existing rules were all
# frozen at endRowIndex=2551, exactly the sheet's size when
# ensure_main_sheet_headers first ran; any row appended past that number
# silently gets zero conditional formatting (no Approve/Deny color, no
# Score highlight, nothing). Every range below now specifies an explicit
# endRowIndex instead of omitting it, so newly appended rows keep being
# covered without needing another manual fix later.
#
# Sizing this turned out to need two live iterations, not one -- 50,000
# rows crashed the Google Sheets mobile app on scroll; reverting to 8,000
# still crashed it. The Sheets *client* (not this pipeline -- every API
# call succeeds normally regardless) has to evaluate all 7 conditional-
# format rules per row as it renders, several of them CUSTOM_FORMULA
# (real per-row formula evaluation, not a cheap static check -- the two
# date-parsing rules in particular do nested MID/LEFT/VALUE/DATE/IFERROR/
# AND per row). Landed on keeping this close to real data size (~2,559
# rows at the time) rather than a large fixed buffer, accepting that it
# needs periodic bumping as the sheet grows rather than being "generously"
# sized once and forgotten -- for anything a Sheets client actively
# evaluates per row, headroom size directly trades off against client
# rendering cost, unlike a passively-stored value where a big buffer is
# free.
CONDITIONAL_FORMAT_ROW_HEADROOM = 3_000

# Substrings checked case-insensitively against the Industry cell. Matching
# firms aren't excluded -- staffing/IT-services companies tend to repost the
# same roles in bulk under different postings, and this is just a visual
# muting so they're easy to scan past on Beacon, not a hard filter.
DEPRIORITIZED_INDUSTRY_KEYWORDS = [
    "staffing", "consulting", "it services", "recruiting", "recruitment",
    "professional services", "managed services", "outsourcing",
]


def get_client(credentials_path: str):
    return gspread.service_account(filename=credentials_path)


def get_main_worksheet(client, sheet_id: str):
    # Wrapped in retry -- unlike every per-cell write call in this module,
    # this initial connection call used to fail immediately on a transient
    # 429/503 rather than retrying, hit live multiple times this session
    # (most recently a 503 mid-restore, and a 429 moments after a heavy
    # write burst). Every other Sheets API call here already goes through
    # call_with_retry; this one just hadn't been updated to match.
    return call_with_retry(client.open_by_key, sheet_id).sheet1


def resolve_main_worksheet(settings):
    """Returns a ready-to-write Beacon worksheet, or None if Google Sheets
    isn't configured -- callers skip all Beacon writes (and the Sheets API
    calls that would go with them) when this returns None."""
    if not settings.google_sheets_credentials_path or not settings.google_sheet_id:
        return None
    client = get_client(settings.google_sheets_credentials_path)
    ws = get_main_worksheet(client, settings.google_sheet_id)
    ensure_main_sheet_headers(ws)
    return ws


def _col_letter(index: int) -> str:
    """1-based column index -> A1 column letter."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


CLEARED_ROW_BACKGROUND = {"red": 1, "green": 1, "blue": 1}


def _date_highlight_color(posted_at: str | None, today_central: date) -> dict | None:
    """Returns the target background color for a job's Date Posted --
    POSTED_TODAY_BLUE, POSTED_THIS_WEEK_GREEN, or None (no highlight) --
    or None if posted_at is missing/unparseable. Computed directly from
    jobs.posted_at (already a real datetime in the DB) rather than parsing
    the *rendered* Sheet text back into a date -- this used to be a live
    CUSTOM_FORMULA conditional-format rule instead, which required every
    Sheets client to reconstruct a date from the "mmddyyyy HH:MM" text via
    nested MID/LEFT/VALUE/DATE calls on every row, every render. That
    turned out to be too expensive for the Google Sheets mobile app to
    evaluate while scrolling -- confirmed live (crashed even after cutting
    the row range down to near-actual-data-size twice; stopped crashing
    entirely once every conditional-format rule was removed as a clean
    test). Computing this once in Python and writing a plain static color
    costs the Sheets client nothing to render -- same as any other
    manually-colored cell."""
    posted = parse_datetime(posted_at)
    if posted is None:
        return None
    posted_central = posted.astimezone(CENTRAL).date()
    if posted_central == today_central:
        return POSTED_TODAY_BLUE
    week_start = today_central - timedelta(days=(today_central.weekday() + 1) % 7)  # this week's Sunday
    week_end = week_start + timedelta(days=6)  # this week's Saturday
    if week_start <= posted_central <= week_end:
        return POSTED_THIS_WEEK_GREEN
    return None


def refresh_date_highlights(conn: sqlite3.Connection, ws) -> dict:
    """Recomputes and re-applies the "posted today"/"posted this week"
    static row background colors for every job currently on Beacon --
    per direct request ("Highlight new jobs posted for that day in light
    blue... After alphabetical sort, it's not easy to figure out [when jobs
    were posted]"), now implemented as static colors instead of a live
    formula (see _date_highlight_color's docstring for why). Needs a full
    refresh every run (not just newly-added rows) since which jobs qualify
    changes daily on its own -- yesterday's "today" blue needs to fade to
    "this week" green, and last Saturday's green needs to clear entirely
    once the new week starts, with no code path ever explicitly triggering
    that transition otherwise. Relies on jobs.sheet_row_number already
    being fresh -- call after resync_sheet_row_numbers/sort_and_resync_main_sheet,
    never before. Meant to be called every main-pipeline cycle."""
    sheet_id = ws.id
    today_central = datetime.now(CENTRAL).date()
    rows = conn.execute(
        "SELECT posted_at, sheet_row_number FROM jobs WHERE sheet_row_number IS NOT NULL"
    ).fetchall()

    requests = []
    colored = 0
    for job in rows:
        color = _date_highlight_color(job["posted_at"], today_central)
        if color is not None:
            colored += 1
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": job["sheet_row_number"] - 1, "endRowIndex": job["sheet_row_number"],
                    "startColumnIndex": 0, "endColumnIndex": len(MAIN_SHEET_COLUMNS),
                },
                "cell": {"userEnteredFormat": {"backgroundColor": color or CLEARED_ROW_BACKGROUND}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        })

    if requests:
        ws.spreadsheet.batch_update({"requests": requests})
    return {"colored": colored, "evaluated": len(rows)}


def _industry_highlight_rule_request(full_row_range: dict, index: int) -> dict:
    industry_col = _col_letter(INDUSTRY_COL_INDEX)
    keyword_pattern = "|".join(DEPRIORITIZED_INDUSTRY_KEYWORDS)
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [full_row_range],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{
                            "userEnteredValue": f'=REGEXMATCH(LOWER(${industry_col}2), "{keyword_pattern}")'
                        }],
                    },
                    "format": {"backgroundColor": INDUSTRY_DEPRIORITIZE_GREY},
                },
            },
            "index": index,
        }
    }


def _salary_estimate_highlight_rule_request(sheet_id, row_headroom: int, index: int) -> dict:
    salary_col_range = {
        "sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": row_headroom,
        "startColumnIndex": SALARY_RANGE_COL_INDEX - 1, "endColumnIndex": SALARY_RANGE_COL_INDEX,
    }
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [salary_col_range],
                "booleanRule": {
                    "condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": "(est.)"}]},
                    "format": {"backgroundColor": SALARY_ESTIMATE_PINK},
                },
            },
            "index": index,
        }
    }


def _apply_row_ranged_formatting(ws, row_headroom: int) -> None:
    """(Re)builds all 3 data-validation dropdown ranges and all 5
    conditional-format rules for a range ending at row_headroom. Shared by
    ensure_main_sheet_headers (initial setup) and ensure_beacon_capacity
    (periodic growth as the sheet fills up) so the two can never drift out
    of sync with each other. add_validation calls are naturally overwriting
    (safe to repeat), but conditional-format rules are NOT -- any existing
    ones are deleted first (highest index first, so deleting doesn't shift
    the indices out from under the next deletion) to avoid stacking
    duplicates on a repeat call."""
    decision_col = _col_letter(DECISION_COL_INDEX)
    my_decision_col = _col_letter(MY_DECISION_COL_INDEX)
    app_status_col = _col_letter(MAIN_SHEET_COLUMNS.index("Application Status") + 1)

    ws.add_validation(
        f"{decision_col}2:{decision_col}{row_headroom}",
        ValidationConditionType.one_of_list,
        DECISION_VALUES,
        showCustomUi=True,
    )
    ws.add_validation(
        f"{my_decision_col}2:{my_decision_col}{row_headroom}",
        ValidationConditionType.one_of_list,
        MY_DECISION_VALUES,
        showCustomUi=True,
    )
    ws.add_validation(
        f"{app_status_col}2:{app_status_col}{row_headroom}",
        ValidationConditionType.one_of_list,
        APPLICATION_STATUS_VALUES,
        showCustomUi=True,
    )

    sheet_id = ws.id
    existing_rule_count = 0
    for sheet in ws.spreadsheet.fetch_sheet_metadata()["sheets"]:
        if sheet["properties"]["sheetId"] == sheet_id:
            existing_rule_count = len(sheet.get("conditionalFormats", []))
            break

    full_row_range = {
        "sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": row_headroom,
        "startColumnIndex": 0, "endColumnIndex": len(MAIN_SHEET_COLUMNS),
    }
    score_col_range = {
        "sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": row_headroom,
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
                            "values": [{"userEnteredValue": f'=${decision_col}2="Approve"'}],
                        },
                        "format": {"backgroundColor": DECISION_APPROVE_GREEN},
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
                            "values": [{"userEnteredValue": f'=${decision_col}2="Deny"'}],
                        },
                        "format": {"backgroundColor": DECISION_DENY_RED},
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
        _industry_highlight_rule_request(full_row_range, index=3),
        _salary_estimate_highlight_rule_request(sheet_id, row_headroom, index=4),
    ]
    ws.spreadsheet.batch_update({"requests": requests})


def ensure_main_sheet_headers(ws) -> bool:
    """Idempotent: only writes/formats if the header row isn't already set up.
    Returns True if it just set things up. Only handles "already correct" and
    "truly empty" -- this schema is meant to be deployed via a full rebuild
    (clear the sheet, then call this), not an incremental in-place migration,
    since the redesign renamed/reordered/dropped enough columns that a
    diff-based partial migration isn't meaningful. If a genuinely new column
    gets appended in the future, incremental migration logic can be
    reintroduced at that time, scoped to that change."""
    existing = ws.row_values(1)
    if existing == MAIN_SHEET_COLUMNS:
        return False

    call_with_retry(ws.update, values=[MAIN_SHEET_COLUMNS], range_name="A1")

    decision_col = _col_letter(DECISION_COL_INDEX)
    ws.format(f"{decision_col}1", {"backgroundColor": DECISION_HEADER_GREEN})

    _apply_row_ranged_formatting(ws, CONDITIONAL_FORMAT_ROW_HEADROOM)
    return True


# How close (in rows) actual data may get to the current row-ranged
# formatting limit before ensure_beacon_capacity grows it. Deliberately a
# SMALL, fixed increment rather than a large one-time jump -- see
# CONDITIONAL_FORMAT_ROW_HEADROOM's docstring for the live incident (a
# 50,000-row, then 8,000-row, buffer both crashed the Google Sheets mobile
# app on scroll) that this design specifically avoids repeating.
_CAPACITY_SAFETY_MARGIN = 300
_CAPACITY_GROWTH_INCREMENT = 500


def ensure_beacon_capacity(ws) -> bool:
    """Keeps the sheet's grid and every row-ranged conditional-format/
    validation rule sized close to actual data, growing in small
    increments as needed. Meant to be called every main-pipeline cycle
    (see app.sheets.sort_and_resync_main_sheet) so growth happens
    automatically in small steps rather than a human needing to notice and
    manually re-widen a stale range (silently stops covering new rows) or
    overcorrecting with a large fixed buffer (crashes the Sheets client
    trying to render/evaluate that many rows' worth of conditional-format
    formulas -- confirmed live, twice, at 50,000 and 8,000 rows).

    Cheap in the common case (one column read to check current data
    extent); only does the heavier delete-and-rebuild-7-rules work when
    actually needed. Returns True if it grew anything, False if existing
    capacity was already sufficient."""
    job_id_values = call_with_retry(ws.col_values, JOB_ID_COL_INDEX)
    data_row_count = len(job_id_values)
    if ws.row_count - data_row_count > _CAPACITY_SAFETY_MARGIN:
        return False

    new_size = data_row_count + _CAPACITY_SAFETY_MARGIN + _CAPACITY_GROWTH_INCREMENT
    ws.resize(rows=new_size)
    _apply_row_ranged_formatting(ws, new_size)
    return True


def find_existing_row(ws, job_id: int) -> int | None:
    """Duplicate-row guard: search the Job ID column for a matching value.
    Returns the 1-based sheet row number, or None if not present."""
    values = call_with_retry(ws.col_values, JOB_ID_COL_INDEX)
    target = str(job_id)
    for i, value in enumerate(values[1:], start=2):  # skip header row
        if str(value) == target:
            return i
    return None


def resync_sheet_row_numbers(conn: sqlite3.Connection, ws) -> int:
    """Re-reads the Job ID column and updates every currently-on-Beacon
    job's `sheet_row_number` to match its actual current row. Needed
    because most Sheets writes (update_visa_flag, update_score,
    update_company_columns, remove_main_row, ...) already re-locate a job's
    row fresh via find_existing_row rather than trusting this cached DB
    value -- but app.approval's read_decision_row/update_reminder_sent_at do
    NOT, reading/writing `job["sheet_row_number"]` directly. That means any
    row deletion anywhere above a job's row (every eviction path: visa-
    restricted, sub-threshold fit score, manual Reject -- all pre-existing,
    independent of sorting) silently drifts every still-present job below
    it out of sync, risking the Approval Poller reading/writing the wrong
    job's Decision/Reminder cells. Also defensively clears
    `sheet_row_number` for any job the DB thinks is on Beacon but that
    isn't actually found in the sheet at all (every eviction call site
    already nulls this itself, but this costs nothing extra given the
    column read already happened). Returns the number of jobs whose stored
    row number changed."""
    job_id_values = call_with_retry(ws.col_values, JOB_ID_COL_INDEX)
    sheet_positions: dict[int, int] = {}
    for i, value in enumerate(job_id_values[1:], start=2):  # skip header row
        value = value.strip()
        if value.isdigit():
            sheet_positions[int(value)] = i

    tracked = conn.execute(
        "SELECT id, sheet_row_number FROM jobs WHERE sheet_row_number IS NOT NULL"
    ).fetchall()

    changed = 0
    for job in tracked:
        new_row = sheet_positions.get(job["id"])
        if new_row != job["sheet_row_number"]:
            conn.execute("UPDATE jobs SET sheet_row_number = ? WHERE id = ?", (new_row, job["id"]))
            changed += 1
    conn.commit()
    return changed


def sort_and_resync_main_sheet(conn: sqlite3.Connection, ws) -> dict:
    """Sorts Beacon by Company (A-Z), preserving the header row, then
    immediately resyncs every job's `sheet_row_number` to match -- sorting
    physically moves every row, so skipping the resync would leave the
    Approval Poller reading/writing stale positions for the entire sheet,
    not just the rows that actually moved. Runs once per main-pipeline
    cycle (per direct request, "sort alphabetically on sheets at the end of
    the job") rather than after every Sheets-writing step, since a full
    sort is a comparatively heavy operation and near-alphabetical order
    between cycles is an acceptable tradeoff -- correctness doesn't depend
    on sort freshness, only on resync freshness (see
    resync_sheet_row_numbers, also called independently after fit-scoring's
    own evictions for that reason).

    Also runs ensure_beacon_capacity every cycle -- cheap when nothing
    needs growing (one column read), and this is the natural place for it
    since it already runs unconditionally on the same schedule this sheet's
    other structural upkeep (row numbers) happens on. Also refreshes the
    "posted today"/"posted this week" static row colors (see
    refresh_date_highlights) -- must run AFTER the resync above, since it
    relies on jobs.sheet_row_number already being current."""
    job_id_values = call_with_retry(ws.col_values, JOB_ID_COL_INDEX)
    last_row = len(job_id_values)
    if last_row >= 3:  # header + at least 2 data rows -- otherwise nothing meaningful to sort
        last_col = _col_letter(len(MAIN_SHEET_COLUMNS))
        call_with_retry(ws.sort, (2, "asc"), range=f"A2:{last_col}{last_row}")

    resynced = resync_sheet_row_numbers(conn, ws)
    capacity_grown = ensure_beacon_capacity(ws)
    date_highlights = refresh_date_highlights(conn, ws)
    return {"resynced": resynced, "capacity_grown": capacity_grown, "date_highlights": date_highlights}


def _job_ids_with_my_decision(ws, eligible_values: set[str]) -> set[int]:
    """Bulk-reads the Job ID and My Decision columns in two calls (not one
    API call per row) and returns the set of job IDs whose My Decision cell
    is one of eligible_values."""
    job_ids = call_with_retry(ws.col_values, JOB_ID_COL_INDEX)
    my_decisions = call_with_retry(ws.col_values, MY_DECISION_COL_INDEX)
    matched = set()
    for idx in range(1, len(job_ids)):  # skip header at index 0
        value = my_decisions[idx].strip() if idx < len(my_decisions) else ""
        if value not in eligible_values:
            continue
        try:
            matched.add(int(job_ids[idx]))
        except ValueError:
            continue
    return matched


def get_scoreable_job_ids(ws) -> set[int]:
    """Job IDs currently eligible for fit-scoring: flagged "Go Score"
    (freshly requested) or "AI Score Pending" (claimed by an earlier run
    whose API call failed -- retried automatically). Replaces the earlier
    Score-Request-column-based get_score_requested_job_ids()."""
    return _job_ids_with_my_decision(ws, {MY_DECISION_GO_SCORE, MY_DECISION_AI_SCORE_PENDING})


def get_rejected_job_ids(ws) -> set[int]:
    """Job IDs the user has flagged My Decision = "Reject" -- evicted to the
    Job Log by app.fit_scoring's rejection pass."""
    return _job_ids_with_my_decision(ws, {MY_DECISION_REJECT})


def build_main_row(
    job: sqlite3.Row,
    company: sqlite3.Row | None,
    score: int | None = None,
    salary_min: int | None = None,
    salary_max: int | None = None,
) -> list:
    row = {col: "" for col in MAIN_SHEET_COLUMNS}
    row["Job ID"] = job["id"]
    row["Company"] = company["name"] if company else ""
    row["Title"] = job["title"]
    row["Industry"] = (company["industry"] if company is not None else None) or ""
    row["Location"] = job["location"] or ""
    row["Location State"] = job["location_state"] or ""
    row["Cloud Platforms"] = job["cloud_platforms"] or ""
    row["Date Posted"] = format_central(job["posted_at"])
    # Real range pulled from JD text (see app.salary_extraction), or
    # Adzuna's estimate as a fallback -- deliberately never conflated with
    # each other, see format_salary_range's docstring.
    row["Salary Range"] = format_salary_range(
        salary_min, salary_max, job["adzuna_salary_min"], job["adzuna_salary_max"]
    )
    row["Visa Flag"] = display_visa_flag(job["visa_flag"])
    row["Decision"] = "Pending"
    row["My Decision"] = MY_DECISION_NEW
    row["Initial Fit Score"] = score if score is not None else ""
    if company is not None:
        row["Employee Count"] = company["employee_count"] or company["employee_count_range"] or ""
        row["Public/Private"] = company["company_type"] or ""
        row["Funding Stage"] = company["funding_stage"] or ""
        row["Revenue/Valuation"] = company["revenue_or_valuation"] or ""
        row["DOL LCA Match"] = company["dol_lca_employer_name"] or ""
        row["Last Sponsored"] = _format_lca_date(company["last_lca_certified_date"])
    row["Apply URL"] = job["apply_url"] or job["url"]
    return [row[col] for col in MAIN_SHEET_COLUMNS]


def _format_lca_date(iso_datetime: str | None) -> str:
    """last_lca_certified_date is stored as a full ISO datetime
    (app.lca_enrichment uses date.isoformat() on a datetime); the Sheet only
    needs the date part."""
    if not iso_datetime:
        return ""
    return iso_datetime.split("T")[0]


def add_job_to_beacon(
    conn: sqlite3.Connection,
    ws,
    job: sqlite3.Row,
    company: sqlite3.Row | None,
    workflow_run_id: int | None = None,
) -> int | None:
    """Adds a job to Beacon the moment it passes the Filter Engine -- no
    waiting on visa-scan or fit-score, both filled in later via
    update_visa_flag()/update_score() as those (separately scheduled) steps
    complete. Salary is extracted from the description only (no page-fetch
    fallback) -- this runs for every passing job, so it has to stay cheap.
    Returns the new row number, or None if the job already had a row
    (duplicate-guarded, e.g. a re-run after a crash)."""
    existing_row = find_existing_row(ws, job["id"])
    if existing_row is not None:
        conn.execute(
            "UPDATE jobs SET sheet_row_number = ?, notified_at = COALESCE(notified_at, CURRENT_TIMESTAMP) WHERE id = ?",
            (existing_row, job["id"]),
        )
        conn.commit()
        return None

    salary_min, salary_max = extract_salary_range(job["description"])
    if salary_min is not None:
        conn.execute(
            "UPDATE jobs SET salary_min = ?, salary_max = ?, salary_source = ? WHERE id = ?",
            (salary_min, salary_max, "job description text", job["id"]),
        )
        # Commit now, before the Sheets call below -- that call can retry/sleep
        # for minutes under quota pressure (see app.sheets_retry), and a SQLite
        # write transaction must never stay open across a slow network call:
        # it holds the DB's single write lock the whole time, which starves
        # any other process trying to write concurrently (hit live: a second
        # CLI command got "database is locked" errors from exactly this).
        conn.commit()

    row_values = build_main_row(job, company, score=None, salary_min=salary_min, salary_max=salary_max)
    # table_range MUST be bounded to the real column width -- without it,
    # gspread/Sheets API auto-detects "the table" against the whole,
    # unbounded sheet name and silently grows gridProperties.columnCount by
    # roughly 1.5-2% on nearly every single append. Confirmed live: this is
    # exactly what inflated col_count into the thousands (twice), eventually
    # tripping the workbook's 10M-cell ceiling and killing the filter step
    # for days. Verified fix live: 8 consecutive real appends with this
    # bound held column count perfectly flat; the same appends without it
    # grew column count on every single call.
    last_col = _col_letter(len(MAIN_SHEET_COLUMNS))
    call_with_retry(
        ws.append_row, row_values, value_input_option="USER_ENTERED",
        table_range=f"A1:{last_col}1",
    )
    new_row_number = find_existing_row(ws, job["id"])

    conn.execute(
        "UPDATE jobs SET sheet_row_number = ?, notified_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_row_number, job["id"]),
    )
    conn.commit()

    if workflow_run_id is not None:
        log_step(
            conn, workflow_run_id=workflow_run_id, job_id=job["id"],
            step_name="beacon_add", step_status="ok",
            detail=f"appended to Beacon row {new_row_number}",
        )
    return new_row_number


def update_company_columns(ws, job_id: int, company: sqlite3.Row) -> int | None:
    """Pushes newly-enriched company fields onto a job's EXISTING Beacon row
    (Industry, plus the contiguous Employee Count..Revenue/Valuation block).
    Needed because enrichment (app.enrichment) only updates the companies
    table -- without this, a company enriched after its job already has a
    Beacon row would never show these fields there (only a brand-new row
    created after enrichment picks them up via build_main_row). Returns the
    row number, or None if the job has no row there (e.g. already evicted)."""
    row_number = find_existing_row(ws, job_id)
    if row_number is None:
        return None

    industry_col = _col_letter(INDUSTRY_COL_INDEX)
    call_with_retry(
        ws.update,
        values=[[company["industry"] or ""]],
        range_name=f"{industry_col}{row_number}",
        value_input_option="USER_ENTERED",
    )

    start_col = _col_letter(EMPLOYEE_COUNT_COL_INDEX)
    end_col = _col_letter(REVENUE_VALUATION_COL_INDEX)
    call_with_retry(
        ws.update,
        values=[[
            company["employee_count"] or company["employee_count_range"] or "",
            company["company_type"] or "",
            company["funding_stage"] or "",
            company["revenue_or_valuation"] or "",
        ]],
        range_name=f"{start_col}{row_number}:{end_col}{row_number}",
        value_input_option="USER_ENTERED",
    )

    lca_start_col = _col_letter(DOL_LCA_MATCH_COL_INDEX)
    lca_end_col = _col_letter(LAST_SPONSORED_COL_INDEX)
    call_with_retry(
        ws.update,
        values=[[
            company["dol_lca_employer_name"] or "",
            _format_lca_date(company["last_lca_certified_date"]),
        ]],
        range_name=f"{lca_start_col}{row_number}:{lca_end_col}{row_number}",
        value_input_option="USER_ENTERED",
    )
    return row_number


def update_salary_range(
    ws, job_id: int, salary_min: int | None, salary_max: int | None,
    adzuna_salary_min: int | None, adzuna_salary_max: int | None,
) -> int | None:
    """Updates the Salary Range cell on a job's existing Beacon row in
    place. Returns the row number, or None if the job has no row there."""
    row_number = find_existing_row(ws, job_id)
    if row_number is None:
        return None
    col = _col_letter(SALARY_RANGE_COL_INDEX)
    display = format_salary_range(salary_min, salary_max, adzuna_salary_min, adzuna_salary_max)
    call_with_retry(
        ws.update, values=[[display]], range_name=f"{col}{row_number}", value_input_option="USER_ENTERED"
    )
    return row_number


def update_visa_flag(ws, job_id: int, visa_flag: str) -> int | None:
    """Updates the Visa Flag cell on a job's existing Beacon row in place,
    translating the internal jobs.visa_flag value to its Sheet-facing label.
    Returns the row number, or None if the job has no row there (e.g. it
    was never added, or was already evicted)."""
    row_number = find_existing_row(ws, job_id)
    if row_number is None:
        return None
    col = _col_letter(VISA_FLAG_COL_INDEX)
    call_with_retry(
        ws.update, values=[[display_visa_flag(visa_flag)]], range_name=f"{col}{row_number}",
        value_input_option="USER_ENTERED",
    )
    return row_number


def update_cloud_platforms(ws, job_id: int, cloud_platforms: str) -> int | None:
    """Updates the Cloud Platforms cell on a job's existing Beacon row in
    place (see app.cloud_platforms_refresh). Returns the row number, or None
    if the job has no row there."""
    row_number = find_existing_row(ws, job_id)
    if row_number is None:
        return None
    col = _col_letter(CLOUD_PLATFORMS_COL_INDEX)
    call_with_retry(
        ws.update, values=[[cloud_platforms]], range_name=f"{col}{row_number}",
        value_input_option="USER_ENTERED",
    )
    return row_number


def update_score(ws, job_id: int, score: int) -> int | None:
    """Updates the Initial Fit Score cell and flips My Decision to
    "AI Scored" on a job's existing Beacon row in place. Returns the row
    number, or None if the job has no row there."""
    row_number = find_existing_row(ws, job_id)
    if row_number is None:
        return None
    col = _col_letter(SCORE_COL_INDEX)
    call_with_retry(
        ws.update, values=[[score]], range_name=f"{col}{row_number}", value_input_option="USER_ENTERED"
    )
    update_my_decision(ws, job_id, MY_DECISION_AI_SCORED)
    return row_number


def update_my_decision(ws, job_id: int, value: str) -> int | None:
    """Updates the My Decision cell on a job's existing Beacon row in place.
    Returns the row number, or None if the job has no row there."""
    row_number = find_existing_row(ws, job_id)
    if row_number is None:
        return None
    col = _col_letter(MY_DECISION_COL_INDEX)
    call_with_retry(
        ws.update, values=[[value]], range_name=f"{col}{row_number}", value_input_option="USER_ENTERED"
    )
    return row_number


def read_decision_row(ws, row_number: int) -> dict:
    """Reads the Decision, Rejection Reason, and Reminder Sent At cells for a
    job's existing Beacon row in a single call (cheaper than three separate
    cell reads) -- used by the Approval Poller."""
    values = call_with_retry(ws.row_values, row_number)

    def _get(index: int) -> str:
        return values[index - 1] if len(values) >= index else ""

    return {
        "decision": _get(DECISION_COL_INDEX),
        "rejection_reason": _get(REJECTION_REASON_COL_INDEX),
        "reminder_sent_at": _get(REMINDER_SENT_AT_COL_INDEX),
    }


def update_reminder_sent_at(ws, row_number: int, timestamp: str) -> None:
    """Writes the stalled-decision reminder timestamp. This edit is itself
    the notification -- Google's native rule fires on the write."""
    col = _col_letter(REMINDER_SENT_AT_COL_INDEX)
    call_with_retry(
        ws.update, values=[[timestamp]], range_name=f"{col}{row_number}", value_input_option="USER_ENTERED"
    )


def remove_main_row(ws, job_id: int) -> bool:
    """Evicts a job's row from Beacon (visa-restricted, scored below
    threshold, or My Decision set to Reject -- see app.job_log for where it
    goes instead). Looks up the row fresh rather than trusting a cached row
    number, since any prior deletion elsewhere in the sheet shifts every row
    below it. Returns True if a row was found and removed."""
    row_number = find_existing_row(ws, job_id)
    if row_number is None:
        return False
    call_with_retry(ws.delete_rows, row_number)
    return True
