from gspread.utils import ValidationConditionType

from app.sheets import (
    APPLICATION_STATUS_VALUES,
    DECISION_VALUES,
    JOB_ID_COL_INDEX,
    MAIN_SHEET_COLUMNS,
    MY_DECISION_AI_SCORE_PENDING,
    MY_DECISION_AI_SCORED,
    MY_DECISION_GO_SCORE,
    MY_DECISION_NEW,
    MY_DECISION_REJECT,
    MY_DECISION_VALUES,
    POSTED_THIS_WEEK_GREEN,
    POSTED_TODAY_BLUE,
    add_job_to_beacon,
    build_main_row,
    display_visa_flag,
    ensure_beacon_capacity,
    ensure_main_sheet_headers,
    find_existing_row,
    format_salary_range,
    get_rejected_job_ids,
    get_scoreable_job_ids,
    read_decision_row,
    refresh_date_highlights,
    remove_main_row,
    resync_sheet_row_numbers,
    sort_and_resync_main_sheet,
    update_company_columns,
    update_my_decision,
    update_reminder_sent_at,
    update_salary_range,
    update_score,
    update_visa_flag,
    _date_highlight_color,
)
from tests.fakes import FakeWorksheet


def _job(conn, **overrides):
    fields = {
        "company_id": None,
        "title": "Solutions Architect",
        "url": "https://example.com/1",
        "apply_url": "https://example.com/1/apply",
        "description": "",
        "location": "Remote - US",
        "visa_flag": None,
        "posted_at": "2026-06-01T00:00:00Z",
        "status": "new",
    }
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    cursor = conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", tuple(fields.values()))
    conn.commit()
    return db_row(conn, cursor.lastrowid)


def db_row(conn, job_id):
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def _company(conn, **overrides):
    fields = {
        "name": "Acme",
        "employee_count": 500,
        "hq_location": "Austin, TX",
        "company_type": "private",
        "funding_stage": "series_c",
        "revenue_or_valuation": "$50M ARR (est.)",
    }
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    cursor = conn.execute(f"INSERT INTO companies ({columns}) VALUES ({placeholders})", tuple(fields.values()))
    conn.commit()
    return cursor.lastrowid


def test_ensure_main_sheet_headers_sets_up_when_empty():
    ws = FakeWorksheet()
    changed = ensure_main_sheet_headers(ws)
    assert changed is True
    assert ws.rows[0] == MAIN_SHEET_COLUMNS
    assert len(ws.formats) == 1  # Decision header background
    assert len(ws.validations) == 3

    decision_validation = ws.validations[0]
    assert decision_validation[1] == ValidationConditionType.one_of_list
    assert decision_validation[2] == DECISION_VALUES

    my_decision_validation = ws.validations[1]
    assert my_decision_validation[2] == MY_DECISION_VALUES

    app_status_validation = ws.validations[2]
    assert app_status_validation[2] == APPLICATION_STATUS_VALUES

    assert len(ws.spreadsheet.batch_update_calls) == 1
    requests = ws.spreadsheet.batch_update_calls[0]["requests"]
    assert len(requests) == 5  # Approve-green, Deny-red, Score-highlight, Industry-grey, salary-estimate-pink


def test_ensure_main_sheet_headers_noop_when_already_set():
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS])
    changed = ensure_main_sheet_headers(ws)
    assert changed is False
    assert ws.formats == []
    assert ws.validations == []
    assert ws.spreadsheet.batch_update_calls == []


def test_ensure_main_sheet_headers_full_rebuild_when_columns_differ():
    # No more incremental diff-based migration -- any mismatch (old schema,
    # partially-set-up sheet, etc.) is treated the same as "empty" and gets
    # a full header/validation/format setup.
    old_columns = MAIN_SHEET_COLUMNS[:-2]
    ws = FakeWorksheet(rows=[old_columns, [""] * len(old_columns)])

    changed = ensure_main_sheet_headers(ws)

    assert changed is True
    assert ws.rows[0] == MAIN_SHEET_COLUMNS
    assert len(ws.validations) == 3
    assert len(ws.spreadsheet.batch_update_calls) == 1
    assert len(ws.spreadsheet.batch_update_calls[0]["requests"]) == 5  # Approve, Deny, Score, Industry, Salary


def test_date_highlight_color_blue_when_posted_today():
    from datetime import date

    today = date(2026, 7, 9)  # Thursday
    posted_today = "2026-07-09T14:30:00Z"
    assert _date_highlight_color(posted_today, today) == POSTED_TODAY_BLUE


def test_date_highlight_color_green_when_posted_this_week_but_not_today():
    from datetime import date

    today = date(2026, 7, 9)  # Thursday
    posted_monday = "2026-07-06T14:30:00Z"  # same calendar week
    assert _date_highlight_color(posted_monday, today) == POSTED_THIS_WEEK_GREEN


def test_date_highlight_color_none_when_posted_last_week():
    from datetime import date

    today = date(2026, 7, 9)  # Thursday
    posted_last_saturday = "2026-07-04T14:30:00Z"  # the *previous* week's Saturday
    assert _date_highlight_color(posted_last_saturday, today) is None


def test_date_highlight_color_uses_sunday_to_saturday_calendar_week():
    from datetime import date

    sunday = date(2026, 7, 5)
    saturday = date(2026, 7, 11)
    today = date(2026, 7, 9)  # same week as both

    # Noon UTC, not midnight -- avoids the UTC/Central day-boundary shift
    # (midnight UTC on Sunday is still Saturday evening in Central time).
    assert _date_highlight_color("2026-07-05T12:00:00Z", today) == POSTED_THIS_WEEK_GREEN
    assert _date_highlight_color("2026-07-11T12:00:00Z", today) == POSTED_THIS_WEEK_GREEN
    assert sunday.weekday() == 6 and saturday.weekday() == 5  # sanity: Sun=6, Sat=5 in Python's Mon=0 scheme


def test_date_highlight_color_none_when_posted_at_missing():
    from datetime import date

    assert _date_highlight_color(None, date(2026, 7, 9)) is None
    assert _date_highlight_color("not a date", date(2026, 7, 9)) is None


def test_refresh_date_highlights_colors_todays_job_blue(db_conn):
    import datetime as dt

    now_utc = dt.datetime.now(dt.timezone.utc)
    job = _job(db_conn, sheet_row_number=5, posted_at=now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS] + [[""] * len(MAIN_SHEET_COLUMNS)] * 4)

    result = refresh_date_highlights(db_conn, ws)

    assert result == {"colored": 1, "evaluated": 1}
    requests = ws.spreadsheet.batch_update_calls[0]["requests"]
    assert len(requests) == 1
    cell_format = requests[0]["repeatCell"]["cell"]["userEnteredFormat"]["backgroundColor"]
    assert cell_format == POSTED_TODAY_BLUE
    assert requests[0]["repeatCell"]["range"]["startRowIndex"] == 4  # sheet_row_number 5 -> 0-based index 4


def test_refresh_date_highlights_clears_jobs_posted_long_ago(db_conn):
    job = _job(db_conn, sheet_row_number=2, posted_at="2020-01-01T00:00:00Z")
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS] + [[""] * len(MAIN_SHEET_COLUMNS)])

    result = refresh_date_highlights(db_conn, ws)

    assert result == {"colored": 0, "evaluated": 1}
    requests = ws.spreadsheet.batch_update_calls[0]["requests"]
    cell_format = requests[0]["repeatCell"]["cell"]["userEnteredFormat"]["backgroundColor"]
    assert cell_format == {"red": 1, "green": 1, "blue": 1}


def test_refresh_date_highlights_skips_jobs_not_on_beacon(db_conn):
    _job(db_conn, sheet_row_number=None)
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS])

    result = refresh_date_highlights(db_conn, ws)

    assert result == {"colored": 0, "evaluated": 0}
    assert ws.spreadsheet.batch_update_calls == []


def test_ensure_beacon_capacity_noop_when_headroom_sufficient():
    # 10 data rows (11 incl. header) with a grid of 1000 -- comfortably
    # more than the 300-row safety margin, so nothing should happen.
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS] + [_row(i) for i in range(10)], row_count=1000)

    grown = ensure_beacon_capacity(ws)

    assert grown is False
    assert ws.row_count == 1000
    assert ws.spreadsheet.batch_update_calls == []


def test_ensure_beacon_capacity_grows_when_within_safety_margin():
    # Real live incident this design specifically exists to prevent: a
    # too-small fixed range silently stops covering new rows, and a too-
    # large one crashes the Sheets mobile app on scroll. Growing in a
    # small increment as data approaches the current limit avoids both.
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS] + [_row(i) for i in range(10)], row_count=15)

    grown = ensure_beacon_capacity(ws)

    assert grown is True
    assert ws.row_count == 11 + 300 + 500  # data_row_count + margin + increment
    assert len(ws.spreadsheet.batch_update_calls) == 1  # one batched request, not one call per rule
    assert len(ws.validations) == 3


def test_ensure_beacon_capacity_replaces_rather_than_stacks_existing_rules():
    from app.sheets import _apply_row_ranged_formatting

    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS] + [_row(i) for i in range(10)], row_count=15)

    _apply_row_ranged_formatting(ws, 1000)
    assert len(ws.spreadsheet.conditional_formats) == 5

    _apply_row_ranged_formatting(ws, 2000)
    assert len(ws.spreadsheet.conditional_formats) == 5  # replaced, not doubled to 10


def test_get_scoreable_job_ids_returns_go_score_and_ai_score_pending():
    rows = [MAIN_SHEET_COLUMNS]
    for job_id, decision in [
        (101, MY_DECISION_GO_SCORE),
        (102, MY_DECISION_NEW),
        (103, MY_DECISION_AI_SCORE_PENDING),
        (104, MY_DECISION_REJECT),
    ]:
        row = [""] * len(MAIN_SHEET_COLUMNS)
        row[JOB_ID_COL_INDEX - 1] = str(job_id)
        row[MAIN_SHEET_COLUMNS.index("My Decision")] = decision
        rows.append(row)
    ws = FakeWorksheet(rows=rows)

    assert get_scoreable_job_ids(ws) == {101, 103}


def test_get_scoreable_job_ids_empty_when_nothing_flagged():
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, ["101"] + [""] * (len(MAIN_SHEET_COLUMNS) - 1)])
    assert get_scoreable_job_ids(ws) == set()


def test_get_scoreable_job_ids_handles_short_rows_from_trailing_blanks():
    # gspread's col_values() truncates trailing blank cells -- a row with
    # nothing set past an early column returns a short list.
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, ["101"]])  # far shorter than My Decision's column index
    assert get_scoreable_job_ids(ws) == set()


def test_get_rejected_job_ids_returns_reject_only():
    rows = [MAIN_SHEET_COLUMNS]
    for job_id, decision in [(101, MY_DECISION_REJECT), (102, MY_DECISION_GO_SCORE)]:
        row = [""] * len(MAIN_SHEET_COLUMNS)
        row[JOB_ID_COL_INDEX - 1] = str(job_id)
        row[MAIN_SHEET_COLUMNS.index("My Decision")] = decision
        rows.append(row)
    ws = FakeWorksheet(rows=rows)

    assert get_rejected_job_ids(ws) == {101}


def test_find_existing_row_finds_match():
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, ["101"] + [""] * (len(MAIN_SHEET_COLUMNS) - 1)])
    assert find_existing_row(ws, 101) == 2


def test_find_existing_row_returns_none_when_absent():
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS])
    assert find_existing_row(ws, 999) is None


def _row(job_id, company=""):
    row = [""] * len(MAIN_SHEET_COLUMNS)
    row[JOB_ID_COL_INDEX - 1] = str(job_id)
    row[1] = company  # Company is column 2
    return row


def test_resync_sheet_row_numbers_updates_drifted_rows(db_conn):
    job_a = _job(db_conn, sheet_row_number=2)
    job_b = _job(db_conn, sheet_row_number=3, url="https://example.com/2")
    # Simulates a deletion above job_b's original row: job_b is now
    # actually at row 2, but the DB still thinks it's at row 3.
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, _row(job_b["id"])])

    changed = resync_sheet_row_numbers(db_conn, ws)

    assert changed == 2  # job_a corrected to None, job_b corrected to 2
    assert db_row(db_conn, job_a["id"])["sheet_row_number"] is None
    assert db_row(db_conn, job_b["id"])["sheet_row_number"] == 2


def test_resync_sheet_row_numbers_leaves_unchanged_rows_alone(db_conn):
    job = _job(db_conn, sheet_row_number=2)
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, _row(job["id"])])

    changed = resync_sheet_row_numbers(db_conn, ws)

    assert changed == 0
    assert db_row(db_conn, job["id"])["sheet_row_number"] == 2


def test_resync_sheet_row_numbers_ignores_jobs_not_tracked_as_on_beacon(db_conn):
    _job(db_conn, sheet_row_number=None)
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS])

    assert resync_sheet_row_numbers(db_conn, ws) == 0


def test_sort_and_resync_main_sheet_sorts_by_company_and_fixes_row_numbers(db_conn):
    job_zebra = _job(db_conn, sheet_row_number=2)
    job_acme = _job(db_conn, sheet_row_number=3, url="https://example.com/2")
    ws = FakeWorksheet(rows=[
        MAIN_SHEET_COLUMNS,
        _row(job_zebra["id"], "Zebra Corp"),
        _row(job_acme["id"], "Acme Inc"),
    ])

    result = sort_and_resync_main_sheet(db_conn, ws)

    assert ws.rows[1][1] == "Acme Inc"
    assert ws.rows[2][1] == "Zebra Corp"
    assert db_row(db_conn, job_acme["id"])["sheet_row_number"] == 2
    assert db_row(db_conn, job_zebra["id"])["sheet_row_number"] == 3
    assert result == {"resynced": 2, "capacity_grown": True, "date_highlights": {"colored": 0, "evaluated": 2}}


def test_sort_and_resync_main_sheet_preserves_header_row(db_conn):
    job_zebra = _job(db_conn, sheet_row_number=2)
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, _row(job_zebra["id"], "Zebra Corp")])

    sort_and_resync_main_sheet(db_conn, ws)

    assert ws.rows[0] == MAIN_SHEET_COLUMNS


def test_sort_and_resync_main_sheet_skips_sort_with_fewer_than_two_data_rows(db_conn):
    job = _job(db_conn, sheet_row_number=2)
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, _row(job["id"], "Only One")])

    result = sort_and_resync_main_sheet(db_conn, ws)

    assert ws.rows[1][1] == "Only One"
    assert result == {"resynced": 0, "capacity_grown": True, "date_highlights": {"colored": 0, "evaluated": 1}}


def test_display_visa_flag_translates_internal_values():
    assert display_visa_flag("restricted") == "No sponsor"
    assert display_visa_flag("sponsors") == "Sponsored"
    assert display_visa_flag("unclear") == "Unclear"
    assert display_visa_flag("no_mention") == "No mention"
    assert display_visa_flag("pending") == "Visa Check Pending"
    assert display_visa_flag(None) == ""


def test_format_salary_range_prefers_real_range_over_adzuna_estimate():
    assert format_salary_range(140000, 180000, 120000, 160000) == "$140,000 - $180,000"


def test_format_salary_range_falls_back_to_adzuna_estimate():
    assert format_salary_range(None, None, 120000, 160000) == "$120,000 - $160,000 (est.)"


def test_format_salary_range_blank_when_neither_present():
    assert format_salary_range(None, None, None, None) == ""


def test_build_main_row_populates_expected_fields(db_conn):
    company_id = _company(db_conn, industry="Integration Platform / iPaaS")
    job = _job(db_conn, company_id=company_id)
    company = db_conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()

    row = build_main_row(job, company, score=75, salary_min=140000, salary_max=180000)
    row_dict = dict(zip(MAIN_SHEET_COLUMNS, row))

    assert row_dict["Job ID"] == job["id"]
    assert row_dict["Company"] == "Acme"
    assert row_dict["Initial Fit Score"] == 75
    assert row_dict["Decision"] == "Pending"
    assert row_dict["My Decision"] == MY_DECISION_NEW
    assert row_dict["Salary Range"] == "$140,000 - $180,000"
    assert row_dict["Employee Count"] == 500
    assert row_dict["Industry"] == "Integration Platform / iPaaS"


def test_build_main_row_blank_score_and_salary_when_not_provided(db_conn):
    job = _job(db_conn)
    row = build_main_row(job, None)
    row_dict = dict(zip(MAIN_SHEET_COLUMNS, row))

    assert row_dict["Initial Fit Score"] == ""
    assert row_dict["Salary Range"] == ""


def test_build_main_row_uses_adzuna_estimate_when_no_real_salary(db_conn):
    job = _job(db_conn, adzuna_salary_min=120000, adzuna_salary_max=160000)
    row = build_main_row(job, None, salary_min=None, salary_max=None)
    row_dict = dict(zip(MAIN_SHEET_COLUMNS, row))

    assert row_dict["Salary Range"] == "$120,000 - $160,000 (est.)"


def test_add_job_to_beacon_appends_immediately_without_a_score(db_conn):
    company_id = _company(db_conn)
    job = _job(db_conn, company_id=company_id, description="Salary: $140,000 - $180,000")

    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS])
    row_number = add_job_to_beacon(db_conn, ws, job, db_conn.execute(
        "SELECT * FROM companies WHERE id = ?", (company_id,)
    ).fetchone())

    assert row_number == 2
    assert len(ws.appended) == 1
    row_dict = dict(zip(MAIN_SHEET_COLUMNS, ws.appended[0]))
    assert row_dict["Initial Fit Score"] == ""  # no waiting on a score
    assert row_dict["Salary Range"] == "$140,000 - $180,000"  # pulled from description, cheap path

    row = db_row(db_conn, job["id"])
    assert row["sheet_row_number"] == 2
    assert row["salary_min"] == 140000
    assert row["notified_at"] is not None  # Approval Poller's reminder-window baseline


def test_add_job_to_beacon_sets_notified_at_once_on_duplicate_rerun(db_conn):
    # A re-run after a crash finds the row already there via find_existing_row
    # -- notified_at must still get backfilled (it's a real gap for jobs added
    # before this existed) but never overwritten once set.
    job = _job(db_conn)
    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job["id"])
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    add_job_to_beacon(db_conn, ws, job, None)
    first = db_row(db_conn, job["id"])["notified_at"]
    assert first is not None

    add_job_to_beacon(db_conn, ws, job, None)
    second = db_row(db_conn, job["id"])["notified_at"]
    assert second == first


def test_add_job_to_beacon_logs_workflow_step(db_conn):
    from app.observability import start_workflow_run

    job = _job(db_conn)
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS])
    run_id = start_workflow_run(db_conn, "main_pipeline")

    add_job_to_beacon(db_conn, ws, job, None, workflow_run_id=run_id)

    step = db_conn.execute(
        "SELECT step_name, step_status FROM step_logs WHERE job_id = ?", (job["id"],)
    ).fetchone()
    assert step["step_name"] == "beacon_add"
    assert step["step_status"] == "ok"


def test_add_job_to_beacon_duplicate_guarded(db_conn):
    job = _job(db_conn)
    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job["id"])
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    result = add_job_to_beacon(db_conn, ws, job, None)

    assert result is None  # signals "already existed"
    assert ws.appended == []
    row = db_row(db_conn, job["id"])
    assert row["sheet_row_number"] == 2


def test_update_company_columns_backfills_an_existing_row(db_conn):
    # The realistic case: a job's Beacon row already exists (created before
    # its company was ever enriched), and enrichment runs later -- the row
    # must get backfilled in place, not silently stay blank forever.
    company_id = _company(
        db_conn, name="LateCo", employee_count=1200, hq_location="Denver, CO",
        company_type="private", funding_stage="series_b",
        revenue_or_valuation="$80M ARR (est.)", industry="Consulting Services",
    )
    job = _job(db_conn, company_id=company_id)
    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job["id"])
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])
    company = db_conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()

    row_number = update_company_columns(ws, job["id"], company)

    assert row_number == 2
    row_dict = dict(zip(MAIN_SHEET_COLUMNS, ws.rows[1]))
    assert row_dict["Employee Count"] == 1200
    assert row_dict["Public/Private"] == "private"
    assert row_dict["Funding Stage"] == "series_b"
    assert row_dict["Revenue/Valuation"] == "$80M ARR (est.)"
    assert row_dict["Industry"] == "Consulting Services"


def test_update_company_columns_returns_none_when_not_on_beacon(db_conn):
    company_id = _company(db_conn)
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS])
    company = db_conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    assert update_company_columns(ws, 999, company) is None


def test_update_salary_range_clears_cell_when_passed_none(db_conn):
    job = _job(db_conn)
    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job["id"])
    row_values[MAIN_SHEET_COLUMNS.index("Salary Range")] = "$140,000 - $180,000"
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    row_number = update_salary_range(ws, job["id"], None, None, None, None)

    assert row_number == 2
    row_dict = dict(zip(MAIN_SHEET_COLUMNS, ws.rows[1]))
    assert row_dict["Salary Range"] == ""


def test_update_salary_range_writes_real_values(db_conn):
    job = _job(db_conn)
    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job["id"])
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    update_salary_range(ws, job["id"], 140000, 180000, None, None)

    row_dict = dict(zip(MAIN_SHEET_COLUMNS, ws.rows[1]))
    assert row_dict["Salary Range"] == "$140,000 - $180,000"


def test_update_salary_range_returns_none_when_not_on_beacon(db_conn):
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS])
    assert update_salary_range(ws, 999, None, None, None, None) is None


def test_update_visa_flag_updates_existing_row_with_display_label(db_conn):
    job = _job(db_conn)
    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job["id"])
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    row_number = update_visa_flag(ws, job["id"], "sponsors")

    assert row_number == 2
    row_dict = dict(zip(MAIN_SHEET_COLUMNS, ws.rows[1]))
    assert row_dict["Visa Flag"] == "Sponsored"


def test_update_visa_flag_returns_none_when_not_on_beacon(db_conn):
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS])
    assert update_visa_flag(ws, 999, "sponsors") is None


def test_update_score_updates_score_and_flips_my_decision(db_conn):
    job = _job(db_conn)
    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job["id"])
    row_values[MAIN_SHEET_COLUMNS.index("My Decision")] = MY_DECISION_AI_SCORE_PENDING
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    row_number = update_score(ws, job["id"], 82)

    assert row_number == 2
    row_dict = dict(zip(MAIN_SHEET_COLUMNS, ws.rows[1]))
    assert row_dict["Initial Fit Score"] == 82
    assert row_dict["My Decision"] == MY_DECISION_AI_SCORED


def test_update_my_decision_updates_existing_row(db_conn):
    job = _job(db_conn)
    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job["id"])
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    row_number = update_my_decision(ws, job["id"], MY_DECISION_GO_SCORE)

    assert row_number == 2
    row_dict = dict(zip(MAIN_SHEET_COLUMNS, ws.rows[1]))
    assert row_dict["My Decision"] == MY_DECISION_GO_SCORE


def test_update_my_decision_returns_none_when_not_on_beacon():
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS])
    assert update_my_decision(ws, 999, MY_DECISION_GO_SCORE) is None


def test_remove_main_row_deletes_and_returns_true(db_conn):
    job = _job(db_conn)
    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job["id"])
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    assert remove_main_row(ws, job["id"]) is True
    assert find_existing_row(ws, job["id"]) is None


def test_remove_main_row_returns_false_when_absent():
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS])
    assert remove_main_row(ws, 999) is False


def test_read_decision_row_reads_all_three_cells():
    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[MAIN_SHEET_COLUMNS.index("Decision")] = "Deny"
    row_values[MAIN_SHEET_COLUMNS.index("Rejection Reason")] = "Comp too low"
    row_values[MAIN_SHEET_COLUMNS.index("Reminder Sent At")] = "07022026 09:00"
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    result = read_decision_row(ws, 2)

    assert result == {
        "decision": "Deny",
        "rejection_reason": "Comp too low",
        "reminder_sent_at": "07022026 09:00",
    }


def test_read_decision_row_blank_when_row_shorter_than_expected():
    # gspread's row_values() truncates trailing blank cells -- a freshly
    # appended row with everything after Decision left blank returns a
    # short list, not one padded out to the full column count.
    short_row = [""] * (MAIN_SHEET_COLUMNS.index("Decision") + 1)
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, short_row])

    result = read_decision_row(ws, 2)

    assert result == {"decision": "", "rejection_reason": "", "reminder_sent_at": ""}


def test_update_reminder_sent_at_writes_the_cell():
    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    update_reminder_sent_at(ws, 2, "07032026 14:30")

    row_dict = dict(zip(MAIN_SHEET_COLUMNS, ws.rows[1]))
    assert row_dict["Reminder Sent At"] == "07032026 14:30"
