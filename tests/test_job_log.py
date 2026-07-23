from app.job_log import (
    DECISION_VALUES,
    JOB_LOG_COLUMNS,
    STAGE_SCORED_BELOW_THRESHOLD,
    STAGE_USER_REJECTED,
    ensure_job_log_headers,
    find_existing_row,
    upsert_job_log_row,
)
from app.sheets import MY_DECISION_AI_SCORED, MY_DECISION_REJECT
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
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)).fetchone()


def _company(conn, **overrides):
    fields = {
        "name": "Acme",
        "employee_count": 500,
        "company_type": "private",
        "funding_stage": "series_c",
        "revenue_or_valuation": "$50M ARR (est.)",
    }
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    cursor = conn.execute(f"INSERT INTO companies ({columns}) VALUES ({placeholders})", tuple(fields.values()))
    conn.commit()
    return conn.execute("SELECT * FROM companies WHERE id = ?", (cursor.lastrowid,)).fetchone()


def test_ensure_job_log_headers_sets_up_when_empty():
    ws = FakeWorksheet()
    changed = ensure_job_log_headers(ws)
    assert changed is True
    assert ws.rows[0] == JOB_LOG_COLUMNS
    assert len(ws.validations) == 1
    assert ws.validations[0][2] == DECISION_VALUES
    assert len(ws.spreadsheet.batch_update_calls) == 1
    requests = ws.spreadsheet.batch_update_calls[0]["requests"]
    assert len(requests) == 3  # Accept-green, Reject-red, Score-highlight


def test_ensure_job_log_headers_noop_when_already_set():
    ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS])
    changed = ensure_job_log_headers(ws)
    assert changed is False
    assert ws.validations == []
    assert ws.spreadsheet.batch_update_calls == []


def test_upsert_job_log_row_appends_when_new(db_conn):
    company = _company(db_conn, industry="Integration Platform / iPaaS")
    job = _job(db_conn, company_id=company["id"])
    ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS])

    upsert_job_log_row(ws, job, company, "Visa Restricted: no sponsorship mentioned", fit_score=75)

    assert len(ws.appended) == 1
    row_dict = dict(zip(JOB_LOG_COLUMNS, ws.appended[0]))
    assert row_dict["Job ID"] == job["id"]
    assert row_dict["Company"] == "Acme"
    assert row_dict["Title"] == "Solutions Architect"
    assert row_dict["Industry"] == "Integration Platform / iPaaS"
    assert row_dict["Reason for Rejection"] == "Visa Restricted: no sponsorship mentioned"
    assert row_dict["Initial Fit Score"] == 75
    assert row_dict["Decision"] == ""  # new jobs start with no decision
    assert row_dict["Last Updated"] != ""


def test_upsert_job_log_row_blank_fit_score_when_not_provided(db_conn):
    job = _job(db_conn)
    ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS])
    upsert_job_log_row(ws, job, None, "Filtered Out - Seniority")
    row_dict = dict(zip(JOB_LOG_COLUMNS, ws.appended[0]))
    assert row_dict["Initial Fit Score"] == ""
    assert row_dict["Company"] == ""


def test_upsert_job_log_row_populates_visa_flag_and_salary(db_conn):
    job = _job(db_conn, visa_flag="restricted", salary_min=140000, salary_max=180000)
    ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS])

    upsert_job_log_row(ws, job, None, "Visa Restricted")

    row_dict = dict(zip(JOB_LOG_COLUMNS, ws.appended[0]))
    assert row_dict["Visa Flag"] == "No sponsor"
    assert row_dict["Salary Range"] == "$140,000 - $180,000"


def test_upsert_job_log_row_updates_existing_row_in_place(db_conn):
    job = _job(db_conn)
    existing = [""] * len(JOB_LOG_COLUMNS)
    existing[JOB_LOG_COLUMNS.index("Job ID")] = job["id"]
    existing[JOB_LOG_COLUMNS.index("Reason for Rejection")] = "Scored Below Threshold"
    ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS, existing])

    upsert_job_log_row(ws, job, None, STAGE_SCORED_BELOW_THRESHOLD, fit_score=45)

    assert ws.appended == []  # no new row
    assert len(ws.rows) == 2
    row_dict = dict(zip(JOB_LOG_COLUMNS, ws.rows[1]))
    assert row_dict["Reason for Rejection"] == STAGE_SCORED_BELOW_THRESHOLD
    assert row_dict["Initial Fit Score"] == 45


def test_upsert_job_log_row_never_overwrites_decision_column(db_conn):
    job = _job(db_conn)
    existing = [""] * len(JOB_LOG_COLUMNS)
    existing[JOB_LOG_COLUMNS.index("Job ID")] = job["id"]
    existing[JOB_LOG_COLUMNS.index("Decision")] = "Accept"
    ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS, existing])

    upsert_job_log_row(ws, job, None, STAGE_USER_REJECTED, my_decision=MY_DECISION_REJECT)

    row_dict = dict(zip(JOB_LOG_COLUMNS, ws.rows[1]))
    assert row_dict["Decision"] == "Accept"  # user's manual decision survives the automated update
    assert row_dict["My Decision"] == MY_DECISION_REJECT


def test_upsert_job_log_row_records_my_decision(db_conn):
    job = _job(db_conn)
    ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS])

    upsert_job_log_row(ws, job, None, "Scored Below Threshold", fit_score=40, my_decision=MY_DECISION_AI_SCORED)

    row_dict = dict(zip(JOB_LOG_COLUMNS, ws.appended[0]))
    assert row_dict["My Decision"] == MY_DECISION_AI_SCORED


def test_find_existing_row_finds_match():
    row = [""] * len(JOB_LOG_COLUMNS)
    row[JOB_LOG_COLUMNS.index("Job ID")] = 101
    ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS, row])
    assert find_existing_row(ws, 101) == 2


def test_find_existing_row_returns_none_when_absent():
    ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS])
    assert find_existing_row(ws, 999) is None
