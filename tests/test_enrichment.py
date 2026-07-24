from app.enrichment import (
    get_fmp_enriched_today_count,
    run_enrichment,
    run_fmp_enrichment,
    run_startuphub_enrichment,
    run_tinyfish_enrichment,
)
from app.sheets import JOB_ID_COL_INDEX, MAIN_SHEET_COLUMNS
from tests.fakes import FakeWorksheet


def _company_row(conn, **overrides):
    fields = {"name": "Acme"}
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    cursor = conn.execute(f"INSERT INTO companies ({columns}) VALUES ({placeholders})", tuple(fields.values()))
    conn.commit()
    return cursor.lastrowid


def _job_row(conn, company_id, sheet_row_number=2, url="https://x/1"):
    # Beacon presence (not jobs.status) is what enrichment scopes on -- see
    # app.enrichment's docstring.
    cursor = conn.execute(
        "INSERT INTO jobs (company_id, title, url, sheet_row_number) VALUES (?, 'Solutions Architect', ?, ?)",
        (company_id, url, sheet_row_number),
    )
    conn.commit()
    return cursor.lastrowid


# --- run_fmp_enrichment ---


def test_run_fmp_enrichment_skips_companies_without_a_job_on_beacon(db_conn):
    company_id = _company_row(db_conn)
    _job_row(db_conn, company_id, sheet_row_number=None)  # never made it onto Beacon

    result = run_fmp_enrichment(db_conn, fmp_api_key="fake-key")
    assert result["evaluated"] == 0


def test_run_fmp_enrichment_skips_already_checked_companies(db_conn):
    company_id = _company_row(db_conn)
    db_conn.execute("UPDATE companies SET financial_data_last_checked = CURRENT_TIMESTAMP WHERE id = ?", (company_id,))
    db_conn.commit()
    _job_row(db_conn, company_id)

    result = run_fmp_enrichment(db_conn, fmp_api_key="fake-key")
    assert result["evaluated"] == 0


def test_run_fmp_enrichment_uses_fmp_when_available(db_conn, monkeypatch):
    company_id = _company_row(db_conn, name="Twilio")
    _job_row(db_conn, company_id)

    fmp_result = {
        "employee_count": 5502, "employee_count_range": None,
        "hq_location": "San Francisco, CA", "company_type": "public",
        "funding_stage": "ipo_public", "revenue_or_valuation": "$31.7B market cap",
        "revenue_valuation_source": "Financial Modeling Prep (symbol: TWLO)", "founded_year": None,
    }
    monkeypatch.setattr("app.enrichment.fetch_fmp_profile", lambda name, key, session=None: fmp_result)

    result = run_fmp_enrichment(db_conn, fmp_api_key="fake-key")

    assert result["enriched_fmp"] == 1
    assert result["no_match_fmp"] == 0

    row = db_conn.execute("SELECT employee_count, company_type FROM companies WHERE id = ?", (company_id,)).fetchone()
    assert row["employee_count"] == 5502
    assert row["company_type"] == "public"


def test_run_fmp_enrichment_preserves_existing_field_when_new_value_is_null(db_conn, monkeypatch):
    company_id = _company_row(db_conn, hq_location="Already Known, TX")
    _job_row(db_conn, company_id)

    fmp_result = {"employee_count": 100, "hq_location": None}
    monkeypatch.setattr("app.enrichment.fetch_fmp_profile", lambda name, key, session=None: fmp_result)

    run_fmp_enrichment(db_conn, fmp_api_key="fake-key")

    row = db_conn.execute("SELECT hq_location FROM companies WHERE id = ?", (company_id,)).fetchone()
    assert row["hq_location"] == "Already Known, TX"


def test_run_fmp_enrichment_skips_entirely_when_no_api_key(db_conn, monkeypatch):
    _company_row(db_conn)
    _job_row(db_conn, 1)

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("fetch_fmp_profile should not be called without an FMP API key")

    monkeypatch.setattr("app.enrichment.fetch_fmp_profile", _should_not_be_called)

    result = run_fmp_enrichment(db_conn, fmp_api_key=None)
    assert result["enriched_fmp"] == 0
    assert result["evaluated"] == 0


def test_run_fmp_enrichment_no_match_still_marks_fmp_checked(db_conn, monkeypatch):
    company_id = _company_row(db_conn, name="GXM Technologies")
    _job_row(db_conn, company_id)

    monkeypatch.setattr("app.enrichment.fetch_fmp_profile", lambda name, key, session=None: None)

    result = run_fmp_enrichment(db_conn, fmp_api_key="fake-fmp-key")

    assert result["enriched_fmp"] == 0
    assert result["no_match_fmp"] == 1

    row = db_conn.execute(
        "SELECT financial_data_last_checked, startuphub_last_checked FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    assert row["financial_data_last_checked"] is not None
    # The FMP pass never touches StartupHub's own tracker -- the two are independent.
    assert row["startuphub_last_checked"] is None


def test_run_fmp_enrichment_pushes_result_onto_existing_beacon_row(db_conn, monkeypatch):
    # The realistic sequence: a job's Beacon row already exists (Filter
    # Engine added it before enrichment ever ran) -- enrichment must backfill
    # that row's company columns, not just the companies table.
    company_id = _company_row(db_conn)
    job_id = _job_row(db_conn, company_id)

    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job_id)
    main_ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    fmp_result = {"employee_count": 250, "hq_location": "Boston, Massachusetts"}
    monkeypatch.setattr("app.enrichment.fetch_fmp_profile", lambda name, key, session=None: fmp_result)

    run_fmp_enrichment(db_conn, fmp_api_key="fake-key", main_ws=main_ws)

    row_dict = dict(zip(MAIN_SHEET_COLUMNS, main_ws.rows[1]))
    assert row_dict["Employee Count"] == 250


def test_run_fmp_enrichment_skips_beacon_push_when_no_worksheet_configured(db_conn, monkeypatch):
    # main_ws=None (Google Sheets not configured) must not raise -- same
    # "skip gracefully" pattern as every other Sheets-writing step.
    company_id = _company_row(db_conn)
    _job_row(db_conn, company_id)

    fmp_result = {"employee_count": 250}
    monkeypatch.setattr("app.enrichment.fetch_fmp_profile", lambda name, key, session=None: fmp_result)

    result = run_fmp_enrichment(db_conn, fmp_api_key="fake-key", main_ws=None)
    assert result["enriched_fmp"] == 1


def test_run_fmp_enrichment_continues_past_a_beacon_push_failure(db_conn, monkeypatch):
    # A live crash during an uncapped StartupHub run showed that a Sheets
    # outage mid-loop (there, a 503 from find_existing_row) would abort the
    # rest of a large batch, even though the per-company fetch calls
    # themselves already had this isolation. The push failure must not stop
    # later companies in the same run from being evaluated.
    for i in range(2):
        company_id = _company_row(db_conn, name=f"Company {i}")
        _job_row(db_conn, company_id, url=f"https://x/{i}")

    monkeypatch.setattr("app.enrichment.fetch_fmp_profile", lambda name, key, session=None: {"employee_count": 1})

    class _ExplodingWorksheet:
        pass

    def _boom(*args, **kwargs):
        raise RuntimeError("Sheets outage")

    monkeypatch.setattr("app.enrichment.update_company_columns", _boom)

    result = run_fmp_enrichment(db_conn, fmp_api_key="fake-key", main_ws=_ExplodingWorksheet())

    assert result["evaluated"] == 2
    assert result["enriched_fmp"] == 2  # DB was still updated for both despite the push failures


def test_run_fmp_enrichment_respects_limit(db_conn, monkeypatch):
    for i in range(3):
        company_id = _company_row(db_conn, name=f"Company {i}")
        _job_row(db_conn, company_id, url=f"https://x/{i}")

    monkeypatch.setattr("app.enrichment.fetch_fmp_profile", lambda name, key, session=None: None)

    result = run_fmp_enrichment(db_conn, fmp_api_key="fake-key", limit=2)
    assert result["evaluated"] == 2


# --- run_startuphub_enrichment ---


def test_run_startuphub_enrichment_skips_companies_without_a_job_on_beacon(db_conn):
    company_id = _company_row(db_conn)
    _job_row(db_conn, company_id, sheet_row_number=None)

    result = run_startuphub_enrichment(db_conn, startuphub_api_key="fake-key")
    assert result["evaluated"] == 0


def test_run_startuphub_enrichment_skips_already_checked_companies(db_conn):
    company_id = _company_row(db_conn)
    db_conn.execute("UPDATE companies SET startuphub_last_checked = CURRENT_TIMESTAMP WHERE id = ?", (company_id,))
    db_conn.commit()
    _job_row(db_conn, company_id)

    result = run_startuphub_enrichment(db_conn, startuphub_api_key="fake-key")
    assert result["evaluated"] == 0


def test_run_startuphub_enrichment_runs_even_when_fmp_already_checked_the_company(db_conn, monkeypatch):
    # The core of the split design: a company already fully checked against
    # FMP (e.g. a confirmed public match, which never gets founded_year from
    # FMP's own profile endpoint) must still be eligible for the StartupHub
    # pass, since the two "checked" trackers are now independent.
    company_id = _company_row(db_conn, name="Workato", company_type="public")
    db_conn.execute("UPDATE companies SET financial_data_last_checked = CURRENT_TIMESTAMP WHERE id = ?", (company_id,))
    db_conn.commit()
    _job_row(db_conn, company_id)

    startuphub_result = {"hq_location": "Mountain View, United States", "founded_year": 2013, "industry": "iPaaS"}
    monkeypatch.setattr("app.enrichment.fetch_startuphub_profile", lambda name, key, session=None: startuphub_result)

    result = run_startuphub_enrichment(db_conn, startuphub_api_key="fake-key")

    assert result["evaluated"] == 1
    assert result["enriched_startuphub"] == 1
    row = db_conn.execute("SELECT founded_year, company_type FROM companies WHERE id = ?", (company_id,)).fetchone()
    assert row["founded_year"] == 2013
    assert row["company_type"] == "public"  # FMP's earlier value untouched


def test_run_startuphub_enrichment_uses_startuphub_when_available(db_conn, monkeypatch):
    company_id = _company_row(db_conn, name="Workato")
    _job_row(db_conn, company_id)

    startuphub_result = {
        "employee_count": None, "employee_count_range": None,
        "hq_location": "Mountain View, United States", "company_type": None,
        "funding_stage": None, "revenue_or_valuation": None,
        "revenue_valuation_source": None, "founded_year": 2013,
        "industry": "iPaaS, Workflow Automation",
    }
    monkeypatch.setattr("app.enrichment.fetch_startuphub_profile", lambda name, key, session=None: startuphub_result)

    result = run_startuphub_enrichment(db_conn, startuphub_api_key="fake-startuphub-key")

    assert result["enriched_startuphub"] == 1
    assert result["no_match_startuphub"] == 0

    row = db_conn.execute(
        "SELECT hq_location, founded_year, industry, employee_count FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    assert row["hq_location"] == "Mountain View, United States"
    assert row["founded_year"] == 2013
    assert row["industry"] == "iPaaS, Workflow Automation"
    # StartupHub's free tier never provides this -- stays blank, no LLM fills it in
    assert row["employee_count"] is None


def test_run_startuphub_enrichment_no_match_still_marks_startuphub_checked(db_conn, monkeypatch):
    company_id = _company_row(db_conn, name="GXM Technologies")
    _job_row(db_conn, company_id)

    monkeypatch.setattr("app.enrichment.fetch_startuphub_profile", lambda name, key, session=None: None)

    result = run_startuphub_enrichment(db_conn, startuphub_api_key="fake-startuphub-key")

    assert result["enriched_startuphub"] == 0
    assert result["no_match_startuphub"] == 1

    row = db_conn.execute(
        "SELECT startuphub_last_checked, financial_data_last_checked FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    assert row["startuphub_last_checked"] is not None
    assert row["financial_data_last_checked"] is None


def test_run_startuphub_enrichment_skips_entirely_when_no_api_key(db_conn, monkeypatch):
    _company_row(db_conn)
    _job_row(db_conn, 1)

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("fetch_startuphub_profile should not be called without a StartupHub API key")

    monkeypatch.setattr("app.enrichment.fetch_startuphub_profile", _should_not_be_called)

    result = run_startuphub_enrichment(db_conn, startuphub_api_key=None)
    assert result["enriched_startuphub"] == 0
    assert result["evaluated"] == 0


def test_run_startuphub_enrichment_has_no_limit_by_default(db_conn, monkeypatch):
    # No published rate limit on StartupHub.ai -- unlike FMP, the default
    # (limit=None) must process the entire eligible backlog in one run.
    for i in range(5):
        company_id = _company_row(db_conn, name=f"Company {i}")
        _job_row(db_conn, company_id, url=f"https://x/{i}")

    monkeypatch.setattr("app.enrichment.fetch_startuphub_profile", lambda name, key, session=None: None)

    result = run_startuphub_enrichment(db_conn, startuphub_api_key="fake-key")
    assert result["evaluated"] == 5


# --- run_tinyfish_enrichment ---


def _both_checked(conn, company_id):
    conn.execute(
        "UPDATE companies SET financial_data_last_checked = CURRENT_TIMESTAMP, "
        "startuphub_last_checked = CURRENT_TIMESTAMP WHERE id = ?",
        (company_id,),
    )
    conn.commit()


def test_run_tinyfish_enrichment_skips_companies_without_a_job_on_beacon(db_conn):
    company_id = _company_row(db_conn)
    _both_checked(db_conn, company_id)
    _job_row(db_conn, company_id, sheet_row_number=None)

    result = run_tinyfish_enrichment(db_conn, tinyfish_api_key="fake-key")
    assert result["evaluated"] == 0


def test_run_tinyfish_enrichment_skips_companies_fmp_and_startuphub_havent_checked_yet(db_conn):
    # The core gating rule: TinyFish must never spend a call on a company
    # the two structured sources haven't had a chance at yet.
    company_id = _company_row(db_conn)
    _job_row(db_conn, company_id)

    result = run_tinyfish_enrichment(db_conn, tinyfish_api_key="fake-key")
    assert result["evaluated"] == 0


def test_run_tinyfish_enrichment_skips_companies_checked_by_only_one_of_the_two(db_conn):
    company_id = _company_row(db_conn)
    db_conn.execute(
        "UPDATE companies SET financial_data_last_checked = CURRENT_TIMESTAMP WHERE id = ?", (company_id,)
    )
    db_conn.commit()
    _job_row(db_conn, company_id)

    result = run_tinyfish_enrichment(db_conn, tinyfish_api_key="fake-key")
    assert result["evaluated"] == 0


def test_run_tinyfish_enrichment_skips_companies_that_already_have_industry(db_conn):
    company_id = _company_row(db_conn, industry="Already Known")
    _both_checked(db_conn, company_id)
    _job_row(db_conn, company_id)

    result = run_tinyfish_enrichment(db_conn, tinyfish_api_key="fake-key")
    assert result["evaluated"] == 0


def test_run_tinyfish_enrichment_skips_already_checked_companies(db_conn):
    company_id = _company_row(db_conn)
    _both_checked(db_conn, company_id)
    db_conn.execute("UPDATE companies SET tinyfish_last_checked = CURRENT_TIMESTAMP WHERE id = ?", (company_id,))
    db_conn.commit()
    _job_row(db_conn, company_id)

    result = run_tinyfish_enrichment(db_conn, tinyfish_api_key="fake-key")
    assert result["evaluated"] == 0


def test_run_tinyfish_enrichment_uses_tinyfish_when_available(db_conn, monkeypatch):
    company_id = _company_row(db_conn, name="SolutionIT")
    _both_checked(db_conn, company_id)
    _job_row(db_conn, company_id)

    tinyfish_result = {
        "industry": "Information Technology & Services",
        "industry_source_url": "https://linkedin.com/company/solutionit",
    }
    monkeypatch.setattr("app.enrichment.fetch_tinyfish_industry", lambda name, key, session=None: tinyfish_result)

    result = run_tinyfish_enrichment(db_conn, tinyfish_api_key="fake-key")

    assert result["enriched_tinyfish"] == 1
    assert result["no_match_tinyfish"] == 0

    row = db_conn.execute(
        "SELECT industry, industry_source_url, tinyfish_last_checked FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    assert row["industry"] == "Information Technology & Services"
    assert row["industry_source_url"] == "https://linkedin.com/company/solutionit"
    assert row["tinyfish_last_checked"] is not None


def test_run_tinyfish_enrichment_no_match_still_marks_tinyfish_checked(db_conn, monkeypatch):
    company_id = _company_row(db_conn, name="Qode")
    _both_checked(db_conn, company_id)
    _job_row(db_conn, company_id)

    monkeypatch.setattr("app.enrichment.fetch_tinyfish_industry", lambda name, key, session=None: None)

    result = run_tinyfish_enrichment(db_conn, tinyfish_api_key="fake-key")

    assert result["enriched_tinyfish"] == 0
    assert result["no_match_tinyfish"] == 1

    row = db_conn.execute(
        "SELECT industry, tinyfish_last_checked FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    assert row["industry"] is None
    assert row["tinyfish_last_checked"] is not None


def test_run_tinyfish_enrichment_skips_entirely_when_no_api_key(db_conn, monkeypatch):
    company_id = _company_row(db_conn)
    _both_checked(db_conn, company_id)
    _job_row(db_conn, company_id)

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("fetch_tinyfish_industry should not be called without a TinyFish API key")

    monkeypatch.setattr("app.enrichment.fetch_tinyfish_industry", _should_not_be_called)

    result = run_tinyfish_enrichment(db_conn, tinyfish_api_key=None)
    assert result["enriched_tinyfish"] == 0
    assert result["evaluated"] == 0


def test_run_tinyfish_enrichment_pushes_result_onto_existing_beacon_row(db_conn, monkeypatch):
    company_id = _company_row(db_conn)
    _both_checked(db_conn, company_id)
    job_id = _job_row(db_conn, company_id)

    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job_id)
    main_ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    tinyfish_result = {"industry": "Construction", "industry_source_url": "https://linkedin.com/x"}
    monkeypatch.setattr("app.enrichment.fetch_tinyfish_industry", lambda name, key, session=None: tinyfish_result)

    run_tinyfish_enrichment(db_conn, tinyfish_api_key="fake-key", main_ws=main_ws)

    row_dict = dict(zip(MAIN_SHEET_COLUMNS, main_ws.rows[1]))
    assert row_dict["Industry"] == "Construction"


# --- get_fmp_enriched_today_count ---


def test_get_fmp_enriched_today_count_counts_companies_checked_today(db_conn, monkeypatch):
    company_id = _company_row(db_conn)
    _job_row(db_conn, company_id)

    assert get_fmp_enriched_today_count(db_conn) == 0

    fmp_result = {"employee_count": 250}
    monkeypatch.setattr("app.enrichment.fetch_fmp_profile", lambda name, key, session=None: fmp_result)
    run_fmp_enrichment(db_conn, fmp_api_key="fake-key")

    assert get_fmp_enriched_today_count(db_conn) == 1


def test_get_fmp_enriched_today_count_counts_no_match_companies_too(db_conn, monkeypatch):
    # financial_data_last_checked is stamped even when FMP has no match --
    # this still counts against today's cumulative usage, since it still
    # cost an FMP call.
    company_id = _company_row(db_conn)
    _job_row(db_conn, company_id)

    monkeypatch.setattr("app.enrichment.fetch_fmp_profile", lambda name, key, session=None: None)
    run_fmp_enrichment(db_conn, fmp_api_key="fake-key")

    assert get_fmp_enriched_today_count(db_conn) == 1


def test_get_fmp_enriched_today_count_ignores_companies_never_checked(db_conn):
    _company_row(db_conn, name="Never Checked Co")
    assert get_fmp_enriched_today_count(db_conn) == 0


def test_get_fmp_enriched_today_count_ignores_startuphub_only_checks(db_conn, monkeypatch):
    # A company checked only against StartupHub (financial_data_last_checked
    # still NULL) must not count against FMP's daily budget.
    company_id = _company_row(db_conn)
    _job_row(db_conn, company_id)

    monkeypatch.setattr("app.enrichment.fetch_startuphub_profile", lambda name, key, session=None: None)
    run_startuphub_enrichment(db_conn, startuphub_api_key="fake-key")

    assert get_fmp_enriched_today_count(db_conn) == 0


# --- run_enrichment (combined, manual/CLI entry point) ---


def test_run_enrichment_runs_both_passes_and_merges_results(db_conn, monkeypatch):
    company_id = _company_row(db_conn, name="Twilio")
    _job_row(db_conn, company_id)

    monkeypatch.setattr("app.enrichment.fetch_fmp_profile", lambda name, key, session=None: {"employee_count": 5502})
    monkeypatch.setattr(
        "app.enrichment.fetch_startuphub_profile", lambda name, key, session=None: {"founded_year": 2008}
    )

    result = run_enrichment(db_conn, {}, fmp_api_key="fake-fmp-key", startuphub_api_key="fake-startuphub-key")

    assert result["evaluated"] == 2  # one company, evaluated once per pass
    assert result["enriched_fmp"] == 1
    assert result["enriched_startuphub"] == 1
    assert result["enriched"] == 2

    row = db_conn.execute(
        "SELECT employee_count, founded_year FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    assert row["employee_count"] == 5502
    assert row["founded_year"] == 2008
