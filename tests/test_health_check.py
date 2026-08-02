from app.health_check import check_health
from app.job_log import JOB_LOG_COLUMNS
from tests.fakes import FakeWorksheet


_next_id = [0]


def _job(conn, **overrides):
    _next_id[0] += 1
    fields = {
        "company_id": None,
        "title": "Solutions Architect",
        "url": f"https://example.com/{_next_id[0]}",
        "apply_url": f"https://example.com/{_next_id[0]}/apply",
        "description": "",
        "location": "Remote - US",
        "visa_flag": None,
        "sheet_row_number": None,
        "status": "new",
    }
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", tuple(fields.values()))
    conn.commit()


def test_check_health_clean_when_nothing_stuck(db_conn):
    _job(db_conn, visa_flag="restricted", sheet_row_number=None)  # already evicted -- fine
    _job(db_conn, visa_flag="sponsors", sheet_row_number=2)  # on Beacon, not restricted -- fine

    result = check_health(db_conn)

    assert result["stuck_restricted_on_beacon"] == 0


def test_check_health_flags_restricted_jobs_still_on_beacon(db_conn):
    _job(db_conn, visa_flag="restricted", sheet_row_number=2)
    _job(db_conn, visa_flag="restricted", sheet_row_number=3)

    result = check_health(db_conn)

    assert result["stuck_restricted_on_beacon"] == 2


def test_check_health_reports_job_log_row_count(db_conn):
    rows = [JOB_LOG_COLUMNS]
    for i in range(5):
        row = [""] * len(JOB_LOG_COLUMNS)
        row[0] = str(i)
        rows.append(row)
    job_log_ws = FakeWorksheet(rows=rows)

    result = check_health(db_conn, job_log_ws=job_log_ws)

    assert result["job_log_row_count"] == 5


def test_check_health_job_log_row_count_none_when_no_worksheet(db_conn):
    result = check_health(db_conn, job_log_ws=None)

    assert result["job_log_row_count"] is None


def test_check_health_job_log_row_count_none_on_sheets_outage(db_conn):
    class _ExplodingWorksheet:
        def col_values(self, *args, **kwargs):
            raise RuntimeError("Sheets outage")

    result = check_health(db_conn, job_log_ws=_ExplodingWorksheet())

    assert result["job_log_row_count"] is None
