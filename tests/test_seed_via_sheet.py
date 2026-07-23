from app.http_client import RequestFailedError
from app.sheets import MAIN_SHEET_COLUMNS
from app.seed_via_sheet import (
    SEED_MARKER,
    _slug_candidates,
    probe_boards,
    run_seed_via_sheet,
)
from tests.fakes import FakeWorksheet


def _seed_row(job_id_value="SEED", company="", title=""):
    row = [""] * len(MAIN_SHEET_COLUMNS)
    row[MAIN_SHEET_COLUMNS.index("Job ID")] = job_id_value
    row[MAIN_SHEET_COLUMNS.index("Company")] = company
    row[MAIN_SHEET_COLUMNS.index("Title")] = title
    return row


def test_slug_candidates_strips_corporate_suffixes_and_squashes_or_hyphenates():
    candidates = _slug_candidates("Acme Corp, Inc.")
    assert "acme" in candidates
    assert "acme-corp-inc" in candidates or "acmecorpinc" in candidates


def test_slug_candidates_handles_single_word_name():
    assert _slug_candidates("Stripe") == ["stripe"]


def test_probe_boards_returns_none_when_nothing_matches(monkeypatch):
    def not_found(slug, **kwargs):
        raise RequestFailedError("404")

    monkeypatch.setattr("app.seed_via_sheet.fetch_greenhouse_jobs", not_found)
    monkeypatch.setattr("app.seed_via_sheet.fetch_lever_jobs", not_found)
    monkeypatch.setattr("app.seed_via_sheet.fetch_ashby_jobs", not_found)
    monkeypatch.setattr("app.seed_via_sheet.fetch_smartrecruiters_jobs", not_found)

    assert probe_boards("Some Totally Unknown Company") is None


def test_probe_boards_accepts_first_verified_greenhouse_match(monkeypatch):
    def fake_greenhouse(slug, **kwargs):
        return [{"url": f"https://job-boards.greenhouse.io/{slug}/jobs/123"}]

    monkeypatch.setattr("app.seed_via_sheet.fetch_greenhouse_jobs", fake_greenhouse)
    monkeypatch.setattr("app.seed_via_sheet.fetch_lever_jobs", lambda slug, **kw: (_ for _ in ()).throw(RequestFailedError("404")))
    monkeypatch.setattr("app.seed_via_sheet.fetch_ashby_jobs", lambda slug, **kw: (_ for _ in ()).throw(RequestFailedError("404")))
    monkeypatch.setattr("app.seed_via_sheet.fetch_smartrecruiters_jobs", lambda slug, **kw: (_ for _ in ()).throw(RequestFailedError("404")))

    result = probe_boards("Acme")
    assert result["source_type"] == "greenhouse"
    assert result["board_url"] == "acme"
    assert result["job_count"] == 1


def test_probe_boards_rejects_unverified_match(monkeypatch):
    # Returns jobs, but their URLs don't contain the guessed slug at all --
    # a stale/misleading response that must not be trusted.
    monkeypatch.setattr(
        "app.seed_via_sheet.fetch_greenhouse_jobs",
        lambda slug, **kw: [{"url": "https://job-boards.greenhouse.io/some-other-company/jobs/999"}],
    )
    monkeypatch.setattr("app.seed_via_sheet.fetch_lever_jobs", lambda slug, **kw: [])
    monkeypatch.setattr("app.seed_via_sheet.fetch_ashby_jobs", lambda slug, **kw: [])
    monkeypatch.setattr("app.seed_via_sheet.fetch_smartrecruiters_jobs", lambda slug, **kw: [])

    assert probe_boards("Acme") is None


def test_probe_boards_falls_through_to_lever_when_greenhouse_fails(monkeypatch):
    monkeypatch.setattr(
        "app.seed_via_sheet.fetch_greenhouse_jobs",
        lambda slug, **kw: (_ for _ in ()).throw(RequestFailedError("404")),
    )
    monkeypatch.setattr(
        "app.seed_via_sheet.fetch_lever_jobs",
        lambda slug, **kw: [{"url": f"https://jobs.lever.co/{slug}/abc-123"}],
    )
    monkeypatch.setattr(
        "app.seed_via_sheet.fetch_ashby_jobs",
        lambda slug, **kw: (_ for _ in ()).throw(RequestFailedError("404")),
    )
    monkeypatch.setattr(
        "app.seed_via_sheet.fetch_smartrecruiters_jobs",
        lambda slug, **kw: (_ for _ in ()).throw(RequestFailedError("404")),
    )

    result = probe_boards("Acme")
    assert result["source_type"] == "lever"


def test_probe_boards_falls_through_to_smartrecruiters_when_others_fail(monkeypatch):
    monkeypatch.setattr(
        "app.seed_via_sheet.fetch_greenhouse_jobs",
        lambda slug, **kw: (_ for _ in ()).throw(RequestFailedError("404")),
    )
    monkeypatch.setattr(
        "app.seed_via_sheet.fetch_lever_jobs",
        lambda slug, **kw: (_ for _ in ()).throw(RequestFailedError("404")),
    )
    monkeypatch.setattr(
        "app.seed_via_sheet.fetch_ashby_jobs",
        lambda slug, **kw: (_ for _ in ()).throw(RequestFailedError("404")),
    )
    monkeypatch.setattr(
        "app.seed_via_sheet.fetch_smartrecruiters_jobs",
        lambda slug, **kw: [{"url": f"https://jobs.smartrecruiters.com/{slug}/123"}],
    )

    result = probe_boards("Acme")
    assert result["source_type"] == "smartrecruiters"


def test_run_seed_via_sheet_returns_empty_without_main_ws(db_conn):
    result = run_seed_via_sheet(db_conn, None)
    assert result == {"processed": 0, "added": 0, "not_found": 0, "cleaned_up": 0}


def test_run_seed_via_sheet_onboards_a_new_company(db_conn, monkeypatch):
    monkeypatch.setattr(
        "app.seed_via_sheet.fetch_greenhouse_jobs",
        lambda slug, **kw: [{"url": f"https://job-boards.greenhouse.io/{slug}/jobs/1"}],
    )
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, _seed_row(company="Acme")])

    result = run_seed_via_sheet(db_conn, ws)

    assert result == {"processed": 1, "added": 1, "not_found": 0, "cleaned_up": 0}
    title = ws.rows[1][MAIN_SHEET_COLUMNS.index("Title")]
    assert title.startswith("Added via Greenhouse")

    company = db_conn.execute("SELECT * FROM companies WHERE name = 'Acme'").fetchone()
    assert company is not None
    assert company["source_type"] == "greenhouse"
    assert company["board_url"] == "acme"
    assert company["priority_tier"] == "A"


def test_run_seed_via_sheet_writes_failure_message_when_no_board_found(db_conn, monkeypatch):
    def not_found(slug, **kwargs):
        raise RequestFailedError("404")

    monkeypatch.setattr("app.seed_via_sheet.fetch_greenhouse_jobs", not_found)
    monkeypatch.setattr("app.seed_via_sheet.fetch_lever_jobs", not_found)
    monkeypatch.setattr("app.seed_via_sheet.fetch_ashby_jobs", not_found)
    monkeypatch.setattr("app.seed_via_sheet.fetch_smartrecruiters_jobs", not_found)

    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, _seed_row(company="Totally Unknown Co")])

    result = run_seed_via_sheet(db_conn, ws)

    assert result["added"] == 0
    assert result["not_found"] == 1
    title = ws.rows[1][MAIN_SHEET_COLUMNS.index("Title")]
    assert "No Greenhouse/Lever/Ashby/SmartRecruiters board found" in title

    assert db_conn.execute("SELECT * FROM companies").fetchall() == []


def test_run_seed_via_sheet_skips_blank_company_name(db_conn):
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, _seed_row(company="")])
    result = run_seed_via_sheet(db_conn, ws)
    assert result == {"processed": 0, "added": 0, "not_found": 0, "cleaned_up": 0}


def test_run_seed_via_sheet_cleans_up_already_processed_row(db_conn):
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, _seed_row(company="Acme", title="Added via Greenhouse (3 open role(s))")])

    result = run_seed_via_sheet(db_conn, ws)

    assert result == {"processed": 0, "added": 0, "not_found": 0, "cleaned_up": 1}
    assert len(ws.rows) == 1  # only the header row left


def test_run_seed_via_sheet_reuses_existing_company_by_normalized_name(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO companies (name) VALUES ('Acme')")
    db_conn.commit()

    monkeypatch.setattr(
        "app.seed_via_sheet.fetch_greenhouse_jobs",
        lambda slug, **kw: [{"url": f"https://job-boards.greenhouse.io/{slug}/jobs/1"}],
    )
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, _seed_row(company="  acme  ")])

    run_seed_via_sheet(db_conn, ws)

    rows = db_conn.execute("SELECT * FROM companies").fetchall()
    assert len(rows) == 1  # no duplicate company row created
    assert rows[0]["source_type"] == "greenhouse"


def test_run_seed_via_sheet_processes_multiple_rows_bottom_to_top(db_conn, monkeypatch):
    def not_found(slug, **kwargs):
        raise RequestFailedError("404")

    monkeypatch.setattr("app.seed_via_sheet.fetch_greenhouse_jobs", not_found)
    monkeypatch.setattr("app.seed_via_sheet.fetch_lever_jobs", not_found)
    monkeypatch.setattr("app.seed_via_sheet.fetch_ashby_jobs", not_found)
    monkeypatch.setattr("app.seed_via_sheet.fetch_smartrecruiters_jobs", not_found)

    ws = FakeWorksheet(
        rows=[
            MAIN_SHEET_COLUMNS,
            _seed_row(company="Already Done", title="Added via Greenhouse (1 open role(s))"),
            _seed_row(company="New Company"),
        ]
    )

    result = run_seed_via_sheet(db_conn, ws)

    assert result["cleaned_up"] == 1
    assert result["processed"] == 1
    assert result["not_found"] == 1
    # only the still-pending row is left, and it got its outcome written
    assert len(ws.rows) == 2
    remaining = dict(zip(MAIN_SHEET_COLUMNS, ws.rows[1]))
    assert remaining["Company"] == "New Company"
    assert "No Greenhouse/Lever/Ashby/SmartRecruiters board found" in remaining["Title"]


def test_run_seed_via_sheet_matches_job_id_case_insensitively(db_conn, monkeypatch):
    monkeypatch.setattr(
        "app.seed_via_sheet.fetch_greenhouse_jobs",
        lambda slug, **kw: [{"url": f"https://job-boards.greenhouse.io/{slug}/jobs/1"}],
    )
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, _seed_row(job_id_value="seed", company="Acme")])

    result = run_seed_via_sheet(db_conn, ws)

    assert result["added"] == 1
    assert SEED_MARKER == "SEED"
