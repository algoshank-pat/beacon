from app.http_client import RequestFailedError
from app.link_check import check_link_dead, run_link_check
from app.sheets import JOB_ID_COL_INDEX, MAIN_SHEET_COLUMNS
from tests.fakes import FakeResponse, FakeSession, FakeWorksheet


def _company(conn, **overrides):
    fields = {"name": "Acme"}
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    cursor = conn.execute(f"INSERT INTO companies ({columns}) VALUES ({placeholders})", tuple(fields.values()))
    conn.commit()
    return cursor.lastrowid


def _job(conn, company_id=None, source_type="adzuna", sheet_row_number=2, url="https://x/1", link_checked_at=None):
    cursor = conn.execute(
        "INSERT INTO jobs (company_id, title, url, source_type, sheet_row_number, status, link_checked_at) "
        "VALUES (?, 'Solutions Architect', ?, ?, ?, 'notified', ?)",
        (company_id, url, source_type, sheet_row_number, link_checked_at),
    )
    conn.commit()
    return cursor.lastrowid


# --- check_link_dead ---


def test_check_link_dead_true_on_adzuna_closed_phrase():
    session = FakeSession([FakeResponse(404, text="...Unfortunately, this job is no longer available...")])
    assert check_link_dead("https://x/1", session=session) is True


def test_check_link_dead_true_on_bare_404_without_the_phrase():
    session = FakeSession([FakeResponse(404, text="<html>Not Found</html>")])
    assert check_link_dead("https://x/1", session=session) is True


def test_check_link_dead_true_on_410_gone():
    session = FakeSession([FakeResponse(410, text="Gone")])
    assert check_link_dead("https://x/1", session=session) is True


def test_check_link_dead_false_when_page_loads_normally():
    session = FakeSession([FakeResponse(200, text="<html>Solutions Architect at Acme...</html>")])
    assert check_link_dead("https://x/1", session=session) is False


def test_check_link_dead_none_on_network_failure():
    session = FakeSession([RequestFailedError("boom")])
    assert check_link_dead("https://x/1", session=session) is None


# --- run_link_check ---


def test_run_link_check_evicts_confirmed_dead_jobs(db_conn, monkeypatch):
    company_id = _company(db_conn)
    job_id = _job(db_conn, company_id=company_id)

    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job_id)
    main_ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])
    job_log_ws = FakeWorksheet(rows=[])

    monkeypatch.setattr("app.link_check.check_link_dead", lambda url, session=None: True)

    result = run_link_check(db_conn, main_ws=main_ws, job_log_ws=job_log_ws)

    assert result == {"checked": 1, "dead": 1}
    row = db_conn.execute("SELECT status, sheet_row_number, link_checked_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "closed"
    assert row["sheet_row_number"] is None
    assert row["link_checked_at"] is not None
    assert len(main_ws.rows) == 1  # header only -- the row was deleted
    assert len(job_log_ws.appended) == 1


def test_run_link_check_leaves_alive_jobs_on_beacon(db_conn, monkeypatch):
    company_id = _company(db_conn)
    job_id = _job(db_conn, company_id=company_id)
    main_ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS])

    monkeypatch.setattr("app.link_check.check_link_dead", lambda url, session=None: False)

    result = run_link_check(db_conn, main_ws=main_ws)

    assert result == {"checked": 1, "dead": 0}
    row = db_conn.execute("SELECT status, sheet_row_number FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "notified"
    assert row["sheet_row_number"] == 2


def test_run_link_check_does_not_evict_on_inconclusive_result(db_conn, monkeypatch):
    company_id = _company(db_conn)
    job_id = _job(db_conn, company_id=company_id)
    main_ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS])

    monkeypatch.setattr("app.link_check.check_link_dead", lambda url, session=None: None)

    result = run_link_check(db_conn, main_ws=main_ws)

    assert result == {"checked": 1, "dead": 0}
    row = db_conn.execute("SELECT status, sheet_row_number, link_checked_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "notified"
    assert row["sheet_row_number"] == 2
    assert row["link_checked_at"] is not None  # still stamped, so it isn't re-checked immediately


def test_run_link_check_skips_targeted_board_sources(db_conn, monkeypatch):
    # app.ingest.detect_closed_jobs already covers greenhouse/lever/ashby
    # via a re-poll-and-diff -- this step must not duplicate that work.
    company_id = _company(db_conn)
    _job(db_conn, company_id=company_id, source_type="greenhouse")

    def _should_not_be_called(url, session=None):
        raise AssertionError("check_link_dead should not be called for a targeted-board source")

    monkeypatch.setattr("app.link_check.check_link_dead", _should_not_be_called)

    result = run_link_check(db_conn)
    assert result == {"checked": 0, "dead": 0}


def test_run_link_check_skips_jobs_not_currently_on_beacon(db_conn, monkeypatch):
    company_id = _company(db_conn)
    _job(db_conn, company_id=company_id, sheet_row_number=None)

    def _should_not_be_called(url, session=None):
        raise AssertionError("check_link_dead should not be called for a job not on Beacon")

    monkeypatch.setattr("app.link_check.check_link_dead", _should_not_be_called)

    result = run_link_check(db_conn)
    assert result == {"checked": 0, "dead": 0}


def test_run_link_check_prioritizes_never_checked_jobs_first(db_conn, monkeypatch):
    company_id = _company(db_conn)
    checked_job = _job(
        db_conn, company_id=company_id, url="https://x/checked",
        link_checked_at="2026-01-01 00:00:00",
    )
    never_checked_job = _job(db_conn, company_id=company_id, url="https://x/never")

    call_order = []
    monkeypatch.setattr(
        "app.link_check.check_link_dead",
        lambda url, session=None: (call_order.append(url), False)[1],
    )

    run_link_check(db_conn, limit=1)

    assert call_order == ["https://x/never"]


def test_run_link_check_respects_limit(db_conn, monkeypatch):
    company_id = _company(db_conn)
    for i in range(3):
        _job(db_conn, company_id=company_id, url=f"https://x/{i}")

    monkeypatch.setattr("app.link_check.check_link_dead", lambda url, session=None: False)

    result = run_link_check(db_conn, limit=2)
    assert result["checked"] == 2
