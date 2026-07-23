from app import ingest as ingest_module
from app.ingest import (
    detect_closed_jobs,
    ingest_adzuna,
    ingest_targeted_company,
    run_ingestion,
    upsert_job,
)


def _sample_job(**overrides):
    job = {
        "title": "Solutions Architect",
        "url": "https://example.com/job/1",
        "apply_url": "https://example.com/job/1",
        "description_html": "<p>Great role</p>",
        "location": "Remote",
        "posted_at": "2026-06-01T00:00:00Z",
        "company_name": "Acme Corp",
        "salary_min": None,
        "salary_max": None,
        "salary_source": None,
        "source_type": "greenhouse",
    }
    job.update(overrides)
    return job


def test_upsert_job_inserts_new_job_and_creates_company(db_conn):
    job_id, outcome = upsert_job(db_conn, _sample_job())
    assert outcome == "inserted"

    row = db_conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["title"] == "Solutions Architect"
    assert row["description"] == "Great role"
    assert row["status"] == "new"

    company = db_conn.execute("SELECT name FROM companies WHERE id = ?", (row["company_id"],)).fetchone()
    assert company["name"] == "Acme Corp"


def test_upsert_job_persists_adzuna_salary_estimate_separately(db_conn):
    job_id, _ = upsert_job(db_conn, _sample_job(
        salary_min=140000, salary_max=180000, salary_source="adzuna",
        adzuna_salary_min=140000, adzuna_salary_max=180000,
    ))
    row = db_conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["adzuna_salary_min"] == 140000
    assert row["adzuna_salary_max"] == 180000


def test_upsert_job_dedups_on_exact_url(db_conn):
    first_id, _ = upsert_job(db_conn, _sample_job())
    second_id, outcome = upsert_job(db_conn, _sample_job(title="Different Title"))
    assert outcome == "duplicate_url"
    assert second_id == first_id

    count = db_conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]
    assert count == 1


def test_upsert_job_flags_fuzzy_duplicate_across_sources(db_conn):
    upsert_job(db_conn, _sample_job(url="https://example.com/gh/1", source_type="greenhouse"))
    job_id, outcome = upsert_job(
        db_conn,
        _sample_job(
            title="Sr Solutions Architect",
            url="https://adzuna.com/redirect/1",
            source_type="adzuna",
        ),
    )
    assert outcome == "duplicate_fuzzy"

    row = db_conn.execute("SELECT status, duplicate_of_job_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "duplicate"
    assert row["duplicate_of_job_id"] is not None

    count = db_conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]
    assert count == 2


def test_detect_closed_jobs_marks_missing_jobs_closed(db_conn):
    db_conn.execute("INSERT INTO companies (id, name, source_type) VALUES (1, 'Acme', 'greenhouse')")
    db_conn.execute(
        "INSERT INTO jobs (company_id, title, url, source_type, status) "
        "VALUES (1, 'A', 'https://x/1', 'greenhouse', 'new')"
    )
    db_conn.execute(
        "INSERT INTO jobs (company_id, title, url, source_type, status) "
        "VALUES (1, 'B', 'https://x/2', 'greenhouse', 'notified')"
    )
    db_conn.commit()

    closed = detect_closed_jobs(db_conn, 1, "greenhouse", current_urls={"https://x/1"})
    assert closed == 1

    statuses = {
        row["url"]: row["status"]
        for row in db_conn.execute("SELECT url, status FROM jobs WHERE company_id = 1")
    }
    assert statuses["https://x/1"] == "new"
    assert statuses["https://x/2"] == "closed"


def test_detect_closed_jobs_ignores_other_sources_and_terminal_states(db_conn):
    db_conn.execute("INSERT INTO companies (id, name, source_type) VALUES (1, 'Acme', 'greenhouse')")
    db_conn.execute(
        "INSERT INTO jobs (company_id, title, url, source_type, status) "
        "VALUES (1, 'A', 'https://x/1', 'adzuna', 'new')"
    )
    db_conn.execute(
        "INSERT INTO jobs (company_id, title, url, source_type, status) "
        "VALUES (1, 'B', 'https://x/2', 'greenhouse', 'approved')"
    )
    db_conn.commit()

    closed = detect_closed_jobs(db_conn, 1, "greenhouse", current_urls=set())
    assert closed == 0


def test_detect_closed_jobs_evicts_from_beacon_when_it_was_on_beacon(db_conn):
    # Real gap found and fixed: this used to only flip jobs.status to
    # 'closed' and stop there -- nothing ever removed the row from Beacon,
    # so a dead link just sat there indefinitely (confirmed live: 14 jobs
    # already marked 'closed' were still showing on the live sheet).
    from tests.fakes import FakeWorksheet
    from app.sheets import MAIN_SHEET_COLUMNS, JOB_ID_COL_INDEX

    db_conn.execute("INSERT INTO companies (id, name, source_type) VALUES (1, 'Acme', 'greenhouse')")
    cursor = db_conn.execute(
        "INSERT INTO jobs (company_id, title, url, source_type, status, sheet_row_number) "
        "VALUES (1, 'B', 'https://x/2', 'greenhouse', 'notified', 5)"
    )
    job_id = cursor.lastrowid
    db_conn.commit()

    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job_id)
    main_ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS] + [[""] * len(MAIN_SHEET_COLUMNS)] * 3 + [row_values])
    job_log_ws = FakeWorksheet(rows=[])

    closed = detect_closed_jobs(db_conn, 1, "greenhouse", current_urls=set(), main_ws=main_ws, job_log_ws=job_log_ws)

    assert closed == 1
    row = db_conn.execute("SELECT status, sheet_row_number FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "closed"
    assert row["sheet_row_number"] is None
    assert len(main_ws.rows) == 4  # the row was deleted
    assert len(job_log_ws.appended) == 1


def test_detect_closed_jobs_no_sheets_io_when_never_on_beacon(db_conn):
    from tests.fakes import FakeWorksheet

    db_conn.execute("INSERT INTO companies (id, name, source_type) VALUES (1, 'Acme', 'greenhouse')")
    db_conn.execute(
        "INSERT INTO jobs (company_id, title, url, source_type, status) "
        "VALUES (1, 'A', 'https://x/1', 'greenhouse', 'new')"
    )
    db_conn.commit()
    main_ws = FakeWorksheet(rows=[])
    job_log_ws = FakeWorksheet(rows=[])

    closed = detect_closed_jobs(db_conn, 1, "greenhouse", current_urls=set(), main_ws=main_ws, job_log_ws=job_log_ws)

    assert closed == 1
    assert main_ws.rows == []  # never touched -- job was never on Beacon
    assert job_log_ws.appended == []


def test_ingest_targeted_company_inserts_and_logs(db_conn, monkeypatch):
    db_conn.execute(
        "INSERT INTO companies (id, name, source_type, board_url) VALUES (1, 'Boomi', 'ashby', 'boomi')"
    )
    db_conn.commit()
    company_row = db_conn.execute("SELECT * FROM companies WHERE id = 1").fetchone()

    fake_jobs = [_sample_job(url="https://jobs.ashbyhq.com/boomi/1", source_type="ashby")]
    monkeypatch.setitem(ingest_module.SOURCE_FETCHERS, "ashby", lambda board_url: fake_jobs)

    from app.observability import start_workflow_run

    run_id = start_workflow_run(db_conn, "main_pipeline")
    result = ingest_targeted_company(db_conn, company_row, workflow_run_id=run_id)

    assert result["inserted"] == 1
    log_count = db_conn.execute(
        "SELECT COUNT(*) AS c FROM step_logs WHERE workflow_run_id = ?", (run_id,)
    ).fetchone()["c"]
    assert log_count == 1


def test_ingest_targeted_company_skips_unconfigured_source(db_conn):
    db_conn.execute(
        "INSERT INTO companies (id, name, source_type, board_url) VALUES (1, 'Salesforce', 'manual', NULL)"
    )
    db_conn.commit()
    company_row = db_conn.execute("SELECT * FROM companies WHERE id = 1").fetchone()

    result = ingest_targeted_company(db_conn, company_row)
    assert result["skipped"] is True


def test_ingest_targeted_company_passes_known_urls_for_smartrecruiters(db_conn, monkeypatch):
    db_conn.execute(
        "INSERT INTO companies (id, name, source_type, board_url) VALUES (1, 'Visa', 'smartrecruiters', 'visa')"
    )
    db_conn.execute(
        "INSERT INTO jobs (company_id, title, url, source_type) VALUES "
        "(1, 'Old Job', 'https://jobs.smartrecruiters.com/visa/1', 'smartrecruiters')"
    )
    db_conn.commit()
    company_row = db_conn.execute("SELECT * FROM companies WHERE id = 1").fetchone()

    captured = {}

    def fake_fetcher(board_url, known_urls=None):
        captured["board_url"] = board_url
        captured["known_urls"] = known_urls
        return []

    monkeypatch.setitem(ingest_module.SOURCE_FETCHERS, "smartrecruiters", fake_fetcher)

    ingest_targeted_company(db_conn, company_row)

    assert captured["board_url"] == "visa"
    assert captured["known_urls"] == {"https://jobs.smartrecruiters.com/visa/1"}


def test_ingest_adzuna_skipped_when_no_active_keywords(db_conn):
    result = ingest_adzuna(db_conn, "app_id", "app_key")
    assert result["skipped"] is True


def test_ingest_adzuna_queries_each_active_keyword(db_conn, monkeypatch):
    db_conn.execute(
        "INSERT INTO filter_keywords (category, keyword) VALUES ('role_keyword_include', 'Solutions Architect')"
    )
    db_conn.execute(
        "INSERT INTO filter_keywords (category, keyword) VALUES ('tech_keyword_include', 'Kafka')"
    )
    db_conn.execute(
        "INSERT INTO filter_keywords (category, keyword, is_active) VALUES ('role_keyword_include', 'Inactive', 0)"
    )
    db_conn.commit()

    calls = []

    def fake_fetch(app_id, app_key, keyword, *, location=None, max_days_old=30):
        calls.append(keyword)
        return [
            _sample_job(
                title=f"{keyword} Role",
                url=f"https://adzuna.com/{keyword}",
                source_type="adzuna",
            )
        ]

    monkeypatch.setattr(ingest_module, "fetch_adzuna_jobs_for_keyword", fake_fetch)

    result = ingest_adzuna(db_conn, "app_id", "app_key")
    assert sorted(calls) == ["Kafka", "Solutions Architect"]
    assert result["inserted"] == 2


def test_run_ingestion_aggregates_totals(db_conn, monkeypatch):
    from app.config import Settings

    db_conn.execute(
        "INSERT INTO companies (id, name, source_type, board_url) VALUES (1, 'Boomi', 'ashby', 'boomi')"
    )
    db_conn.commit()

    monkeypatch.setitem(
        ingest_module.SOURCE_FETCHERS,
        "ashby",
        lambda board_url: [_sample_job(url="https://jobs.ashbyhq.com/boomi/1", source_type="ashby")],
    )

    settings = Settings(
        database_path=None,
        anthropic_api_key=None,
        adzuna_app_id=None,
        adzuna_app_key=None,
        google_sheet_id=None,
        google_job_log_sheet_id=None,
        google_sheets_credentials_path=None,
        claude_desktop_project_id=None,
    )

    result = run_ingestion(db_conn, settings)
    assert result["adzuna"]["skipped"] is True
    assert result["total_inserted"] == 1

    run_row = db_conn.execute(
        "SELECT status, jobs_ingested FROM workflow_runs WHERE id = ?", (result["workflow_run_id"],)
    ).fetchone()
    assert run_row["status"] == "completed"
    assert run_row["jobs_ingested"] == 1
