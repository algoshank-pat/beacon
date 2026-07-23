from app.cloud_platforms_refresh import run_cloud_platforms_refresh
from app.sheets import CLOUD_PLATFORMS_COL_INDEX, JOB_ID_COL_INDEX, MAIN_SHEET_COLUMNS
from tests.fakes import FakeWorksheet

_TRUNCATED_DESCRIPTION = "x" * 500
_FULL_DESCRIPTION = "a real, non-truncated description with no cloud mention"


def _company(conn, **overrides):
    fields = {"name": "Acme"}
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    cursor = conn.execute(f"INSERT INTO companies ({columns}) VALUES ({placeholders})", tuple(fields.values()))
    conn.commit()
    return cursor.lastrowid


def _job(
    conn, company_id=None, sheet_row_number=2, url="https://x/1",
    description=_TRUNCATED_DESCRIPTION, cloud_platforms=None, cloud_platforms_checked_at=None,
):
    cursor = conn.execute(
        "INSERT INTO jobs (company_id, title, url, description, sheet_row_number, status, "
        "cloud_platforms, cloud_platforms_checked_at) VALUES (?, 'Solutions Architect', ?, ?, ?, 'notified', ?, ?)",
        (company_id, url, description, sheet_row_number, cloud_platforms, cloud_platforms_checked_at),
    )
    conn.commit()
    return cursor.lastrowid


def test_run_cloud_platforms_refresh_finds_platform_on_full_page(db_conn, monkeypatch):
    company_id = _company(db_conn)
    job_id = _job(db_conn, company_id=company_id)

    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job_id)
    main_ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    monkeypatch.setattr(
        "app.cloud_platforms_refresh.fetch_job_page_text",
        lambda url, session=None: "Must know AWS and GCP.",
    )

    result = run_cloud_platforms_refresh(db_conn, main_ws=main_ws)

    assert result == {"checked": 1, "found": 1}
    row = db_conn.execute(
        "SELECT cloud_platforms, cloud_platforms_checked_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row["cloud_platforms"] == "AWS, GCP"
    assert row["cloud_platforms_checked_at"] is not None


def test_run_cloud_platforms_refresh_marks_checked_even_when_nothing_found(db_conn, monkeypatch):
    company_id = _company(db_conn)
    job_id = _job(db_conn, company_id=company_id)
    main_ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS])

    monkeypatch.setattr(
        "app.cloud_platforms_refresh.fetch_job_page_text",
        lambda url, session=None: "No cloud mention here.",
    )

    result = run_cloud_platforms_refresh(db_conn, main_ws=main_ws)

    assert result == {"checked": 1, "found": 0}
    row = db_conn.execute(
        "SELECT cloud_platforms, cloud_platforms_checked_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row["cloud_platforms"] is None
    assert row["cloud_platforms_checked_at"] is not None


def test_run_cloud_platforms_refresh_handles_unfetchable_page(db_conn, monkeypatch):
    company_id = _company(db_conn)
    job_id = _job(db_conn, company_id=company_id)

    def _raise(url, session=None):
        raise RuntimeError("unfetchable")

    monkeypatch.setattr("app.cloud_platforms_refresh.fetch_job_page_text", _raise)

    result = run_cloud_platforms_refresh(db_conn)

    assert result == {"checked": 1, "found": 0}
    row = db_conn.execute("SELECT cloud_platforms_checked_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["cloud_platforms_checked_at"] is not None


def test_run_cloud_platforms_refresh_skips_non_truncated_descriptions(db_conn, monkeypatch):
    company_id = _company(db_conn)
    _job(db_conn, company_id=company_id, description=_FULL_DESCRIPTION)

    def _should_not_be_called(url, session=None):
        raise AssertionError("fetch_job_page_text should not be called for a non-truncated description")

    monkeypatch.setattr("app.cloud_platforms_refresh.fetch_job_page_text", _should_not_be_called)

    result = run_cloud_platforms_refresh(db_conn)
    assert result == {"checked": 0, "found": 0}


def test_run_cloud_platforms_refresh_skips_already_checked_jobs(db_conn, monkeypatch):
    company_id = _company(db_conn)
    _job(db_conn, company_id=company_id, cloud_platforms_checked_at="2026-07-01 00:00:00")

    def _should_not_be_called(url, session=None):
        raise AssertionError("fetch_job_page_text should not be called for an already-checked job")

    monkeypatch.setattr("app.cloud_platforms_refresh.fetch_job_page_text", _should_not_be_called)

    result = run_cloud_platforms_refresh(db_conn)
    assert result == {"checked": 0, "found": 0}


def test_run_cloud_platforms_refresh_skips_jobs_not_on_beacon(db_conn, monkeypatch):
    company_id = _company(db_conn)
    _job(db_conn, company_id=company_id, sheet_row_number=None)

    def _should_not_be_called(url, session=None):
        raise AssertionError("fetch_job_page_text should not be called for a job not on Beacon")

    monkeypatch.setattr("app.cloud_platforms_refresh.fetch_job_page_text", _should_not_be_called)

    result = run_cloud_platforms_refresh(db_conn)
    assert result == {"checked": 0, "found": 0}


def test_run_cloud_platforms_refresh_respects_limit(db_conn, monkeypatch):
    company_id = _company(db_conn)
    for i in range(3):
        _job(db_conn, company_id=company_id, url=f"https://x/{i}")

    monkeypatch.setattr(
        "app.cloud_platforms_refresh.fetch_job_page_text", lambda url, session=None: "nothing relevant"
    )

    result = run_cloud_platforms_refresh(db_conn, limit=2)
    assert result["checked"] == 2


def test_run_cloud_platforms_refresh_pushes_result_to_beacon_row(db_conn, monkeypatch):
    company_id = _company(db_conn)
    job_id = _job(db_conn, company_id=company_id)

    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job_id)
    main_ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    monkeypatch.setattr(
        "app.cloud_platforms_refresh.fetch_job_page_text", lambda url, session=None: "Runs on Azure."
    )

    run_cloud_platforms_refresh(db_conn, main_ws=main_ws)

    row_dict = dict(zip(MAIN_SHEET_COLUMNS, main_ws.rows[1]))
    assert row_dict["Cloud Platforms"] == "Azure"
    assert CLOUD_PLATFORMS_COL_INDEX  # constant exists and is importable
