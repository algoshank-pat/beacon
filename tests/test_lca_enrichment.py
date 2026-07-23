from datetime import datetime

from app.lca_enrichment import (
    _extract_certifications,
    parse_lca_disclosure_file,
    run_lca_enrichment,
)


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
