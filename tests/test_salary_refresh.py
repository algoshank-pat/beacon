from app.salary_refresh import run_salary_refresh
from app.sheets import JOB_ID_COL_INDEX, MAIN_SHEET_COLUMNS
from tests.fakes import FakeWorksheet


def _company(conn, **overrides):
    fields = {"name": "Acme"}
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    cursor = conn.execute(f"INSERT INTO companies ({columns}) VALUES ({placeholders})", tuple(fields.values()))
    conn.commit()
    return cursor.lastrowid


def _job(conn, company_id=None, sheet_row_number=2, url="https://x/1", salary_min=None, salary_checked_at=None):
    cursor = conn.execute(
        "INSERT INTO jobs (company_id, title, url, description, sheet_row_number, status, salary_min, salary_checked_at) "
        "VALUES (?, 'Solutions Architect', ?, 'short truncated description', ?, 'notified', ?, ?)",
        (company_id, url, sheet_row_number, salary_min, salary_checked_at),
    )
    conn.commit()
    return cursor.lastrowid


def test_run_salary_refresh_finds_salary_on_full_page(db_conn, monkeypatch):
    company_id = _company(db_conn)
    job_id = _job(db_conn, company_id=company_id)

    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job_id)
    main_ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    monkeypatch.setattr("app.salary_refresh.resolve_posted_salary", lambda description, url: (85000, 95000))

    result = run_salary_refresh(db_conn, main_ws=main_ws)

    assert result == {"checked": 1, "found": 1}
    row = db_conn.execute(
        "SELECT salary_min, salary_max, salary_source, salary_checked_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row["salary_min"] == 85000
    assert row["salary_max"] == 95000
    assert row["salary_source"] == "job posting page"
    assert row["salary_checked_at"] is not None


def test_run_salary_refresh_marks_checked_even_when_no_salary_found(db_conn, monkeypatch):
    company_id = _company(db_conn)
    job_id = _job(db_conn, company_id=company_id)
    main_ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS])

    monkeypatch.setattr("app.salary_refresh.resolve_posted_salary", lambda description, url: (None, None))

    result = run_salary_refresh(db_conn, main_ws=main_ws)

    assert result == {"checked": 1, "found": 0}
    row = db_conn.execute("SELECT salary_min, salary_checked_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["salary_min"] is None
    assert row["salary_checked_at"] is not None


def test_run_salary_refresh_skips_jobs_with_a_real_salary_already(db_conn, monkeypatch):
    company_id = _company(db_conn)
    _job(db_conn, company_id=company_id, salary_min=100000)

    def _should_not_be_called(description, url):
        raise AssertionError("resolve_posted_salary should not be called when salary_min is already set")

    monkeypatch.setattr("app.salary_refresh.resolve_posted_salary", _should_not_be_called)

    result = run_salary_refresh(db_conn)
    assert result == {"checked": 0, "found": 0}


def test_run_salary_refresh_skips_already_checked_jobs(db_conn, monkeypatch):
    company_id = _company(db_conn)
    _job(db_conn, company_id=company_id, salary_checked_at="2026-07-01 00:00:00")

    def _should_not_be_called(description, url):
        raise AssertionError("resolve_posted_salary should not be called for an already-checked job")

    monkeypatch.setattr("app.salary_refresh.resolve_posted_salary", _should_not_be_called)

    result = run_salary_refresh(db_conn)
    assert result == {"checked": 0, "found": 0}


def test_run_salary_refresh_skips_jobs_not_on_beacon(db_conn, monkeypatch):
    company_id = _company(db_conn)
    _job(db_conn, company_id=company_id, sheet_row_number=None)

    def _should_not_be_called(description, url):
        raise AssertionError("resolve_posted_salary should not be called for a job not on Beacon")

    monkeypatch.setattr("app.salary_refresh.resolve_posted_salary", _should_not_be_called)

    result = run_salary_refresh(db_conn)
    assert result == {"checked": 0, "found": 0}


def test_run_salary_refresh_respects_limit(db_conn, monkeypatch):
    company_id = _company(db_conn)
    for i in range(3):
        _job(db_conn, company_id=company_id, url=f"https://x/{i}")

    monkeypatch.setattr("app.salary_refresh.resolve_posted_salary", lambda description, url: (None, None))

    result = run_salary_refresh(db_conn, limit=2)
    assert result["checked"] == 2


def test_run_salary_refresh_pushes_result_to_beacon_row(db_conn, monkeypatch):
    company_id = _company(db_conn)
    job_id = _job(db_conn, company_id=company_id)

    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job_id)
    main_ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    monkeypatch.setattr("app.salary_refresh.resolve_posted_salary", lambda description, url: (85000, 95000))

    run_salary_refresh(db_conn, main_ws=main_ws)

    row_dict = dict(zip(MAIN_SHEET_COLUMNS, main_ws.rows[1]))
    assert row_dict["Salary Range"] == "$85,000 - $95,000"
