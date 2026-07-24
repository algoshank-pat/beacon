from datetime import datetime

from app.lca_enrichment import (
    _extract_certifications,
    merge_lca_data,
    parse_lca_disclosure_file,
    run_lca_enrichment,
)
from app.sheets import JOB_ID_COL_INDEX, MAIN_SHEET_COLUMNS
from tests.fakes import FakeWorksheet


def test_extract_certifications_keeps_most_recent_per_employer():
    rows = [
        ("Certified", datetime(2025, 1, 1), "Acme Inc."),
        ("Certified", datetime(2026, 3, 1), "Acme Inc."),
        ("Denied", datetime(2026, 6, 1), "Acme Inc."),  # newer date, but not Certified -- ignored
    ]
    result = _extract_certifications(rows)
    decision_date, raw_name, status = result["acme inc."]
    assert decision_date == datetime(2026, 3, 1)
    assert raw_name == "Acme Inc."
    assert status == "Certified"


def test_extract_certifications_accepts_certified_withdrawn():
    rows = [("Certified - Withdrawn", datetime(2026, 1, 1), "Beta LLC")]
    result = _extract_certifications(rows)
    assert "beta llc" in result


def test_extract_certifications_skips_non_certified_and_blank_rows():
    rows = [
        ("Denied", datetime(2026, 1, 1), "Gamma Corp"),
        ("Withdrawn", datetime(2026, 1, 1), "Delta Corp"),
        (None, None, None),
        ("Certified", datetime(2026, 1, 1), None),
    ]
    result = _extract_certifications(rows)
    assert result == {}


def test_extract_certifications_normalizes_name_for_the_key():
    rows = [("Certified", datetime(2026, 1, 1), "  ACME   Inc.  ")]
    result = _extract_certifications(rows)
    assert "acme inc." in result


def test_merge_lca_data_keeps_most_recent_decision_across_files():
    fy2025 = _extract_certifications([("Certified", datetime(2025, 6, 1), "Acme Inc.")])
    fy2026 = _extract_certifications([("Certified", datetime(2026, 3, 1), "ACME INC.")])

    merged = merge_lca_data(fy2025, fy2026)

    decision_date, raw_name, status = merged["acme inc."]
    assert decision_date == datetime(2026, 3, 1)
    assert raw_name == "ACME INC."
    assert status == "Certified"


def test_merge_lca_data_keeps_older_files_own_match_when_newer_file_lacks_it():
    fy2025 = _extract_certifications([("Certified", datetime(2025, 6, 1), "Beta LLC")])
    fy2026 = _extract_certifications([("Certified", datetime(2026, 3, 1), "Gamma Corp")])

    merged = merge_lca_data(fy2025, fy2026)

    assert merged["beta llc"][0] == datetime(2025, 6, 1)
    assert merged["gamma corp"][0] == datetime(2026, 3, 1)


def test_merge_lca_data_with_single_file_is_a_no_op():
    single = _extract_certifications([("Certified", datetime(2026, 3, 1), "Acme Inc.")])
    assert merge_lca_data(single) == single


def test_run_lca_enrichment_updates_matching_companies(db_conn):
    db_conn.execute("INSERT INTO companies (name) VALUES ('Acme Inc.')")
    db_conn.execute("INSERT INTO companies (name) VALUES ('Totally Unrelated Co')")
    db_conn.commit()

    parsed = _extract_certifications([("Certified", datetime(2026, 3, 1), "ACME INC.")])
    result = run_lca_enrichment(db_conn, parsed)

    assert result == {"companies_checked": 2, "matched": 1}
    row = db_conn.execute("SELECT * FROM companies WHERE name = 'Acme Inc.'").fetchone()
    assert row["dol_lca_employer_name"] == "ACME INC."
    assert row["last_lca_certified_date"] == "2026-03-01T00:00:00"

    unrelated = db_conn.execute("SELECT * FROM companies WHERE name = 'Totally Unrelated Co'").fetchone()
    assert unrelated["dol_lca_employer_name"] is None
    assert unrelated["last_lca_certified_date"] is None


def test_run_lca_enrichment_does_not_regress_a_newer_stored_date(db_conn):
    db_conn.execute(
        "INSERT INTO companies (name, dol_lca_employer_name, last_lca_certified_date) "
        "VALUES ('Acme Inc.', 'ACME INC.', '2026-03-01T00:00:00')"
    )
    db_conn.commit()

    older_hit = _extract_certifications([("Certified", datetime(2025, 1, 1), "Acme Inc.")])
    result = run_lca_enrichment(db_conn, older_hit)

    assert result == {"companies_checked": 1, "matched": 0}
    row = db_conn.execute("SELECT * FROM companies WHERE name = 'Acme Inc.'").fetchone()
    assert row["dol_lca_employer_name"] == "ACME INC."
    assert row["last_lca_certified_date"] == "2026-03-01T00:00:00"


def test_run_lca_enrichment_updates_when_new_date_is_more_recent(db_conn):
    db_conn.execute(
        "INSERT INTO companies (name, dol_lca_employer_name, last_lca_certified_date) "
        "VALUES ('Acme Inc.', 'Acme Inc.', '2025-01-01T00:00:00')"
    )
    db_conn.commit()

    newer_hit = _extract_certifications([("Certified", datetime(2026, 3, 1), "ACME INC.")])
    result = run_lca_enrichment(db_conn, newer_hit)

    assert result == {"companies_checked": 1, "matched": 1}
    row = db_conn.execute("SELECT * FROM companies WHERE name = 'Acme Inc.'").fetchone()
    assert row["dol_lca_employer_name"] == "ACME INC."
    assert row["last_lca_certified_date"] == "2026-03-01T00:00:00"


def test_run_lca_enrichment_pushes_match_to_existing_beacon_row(db_conn):
    db_conn.execute("INSERT INTO companies (name) VALUES ('Acme Inc.')")
    db_conn.commit()
    company_id = db_conn.execute("SELECT id FROM companies WHERE name = 'Acme Inc.'").fetchone()["id"]
    db_conn.execute(
        "INSERT INTO jobs (company_id, title, url, sheet_row_number) VALUES (?, 'SA', 'https://x/1', 2)",
        (company_id,),
    )
    db_conn.commit()
    job_id = db_conn.execute("SELECT id FROM jobs WHERE url = 'https://x/1'").fetchone()["id"]

    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job_id)
    ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    parsed = _extract_certifications([("Certified", datetime(2026, 3, 1), "ACME INC.")])
    result = run_lca_enrichment(db_conn, parsed, main_ws=ws)

    assert result == {"companies_checked": 1, "matched": 1}
    row_dict = dict(zip(MAIN_SHEET_COLUMNS, ws.rows[1]))
    assert row_dict["DOL LCA Match"] == "ACME INC."
    assert row_dict["Last Sponsored"] == "2026-03-01"


def test_run_lca_enrichment_handles_no_matches(db_conn):
    db_conn.execute("INSERT INTO companies (name) VALUES ('Nobody Sponsors Me Inc.')")
    db_conn.commit()

    result = run_lca_enrichment(db_conn, {})
    assert result == {"companies_checked": 1, "matched": 0}


def test_parse_lca_disclosure_file_reads_real_xlsx_by_header_name(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    # Deliberately out of the real file's column order, to prove this reads
    # by header name, not position.
    ws.append(["EMPLOYER_NAME", "CASE_STATUS", "DECISION_DATE", "SOME_OTHER_COLUMN"])
    ws.append(["Acme Inc.", "Certified", datetime(2026, 3, 1), "ignored"])
    ws.append(["Denied Co", "Denied", datetime(2026, 3, 1), "ignored"])
    path = tmp_path / "lca_sample.xlsx"
    wb.save(path)

    result = parse_lca_disclosure_file(str(path))

    assert "acme inc." in result
    assert "denied co" not in result


def test_parse_lca_disclosure_file_raises_on_missing_expected_column(tmp_path):
    import openpyxl
    import pytest

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["EMPLOYER_NAME", "SOME_OTHER_COLUMN"])
    ws.append(["Acme Inc.", "x"])
    path = tmp_path / "lca_bad.xlsx"
    wb.save(path)

    with pytest.raises(ValueError, match="missing expected column"):
        parse_lca_disclosure_file(str(path))
