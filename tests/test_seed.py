import pytest

from app.seed import load_seed_file, seed_companies

SEED_YAML = """
companies:
  - name: "Acme Corp"
    board_url: "https://boards.greenhouse.io/acme"
    source_type: "greenhouse"
    priority_tier: "A"
    is_favorite: true
    hq_location: "Remote"
    founded_year: 2015
  - name: "Beta Inc"
    source_type: "manual"
    priority_tier: "B"
"""

SEED_YAML_MISSING_NAME = """
companies:
  - board_url: "https://boards.greenhouse.io/noname"
"""


@pytest.fixture
def seed_file(tmp_path):
    path = tmp_path / "seed_companies.yaml"
    path.write_text(SEED_YAML, encoding="utf-8")
    return path


def test_load_seed_file_parses_companies(seed_file):
    companies = load_seed_file(seed_file)
    assert len(companies) == 2
    assert companies[0]["name"] == "Acme Corp"


def test_load_seed_file_requires_name(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(SEED_YAML_MISSING_NAME, encoding="utf-8")
    with pytest.raises(ValueError):
        load_seed_file(path)


def test_seed_companies_inserts_new_rows(db_conn, seed_file):
    result = seed_companies(db_conn, seed_file)
    assert result == {"inserted": 2, "updated": 0}

    row = db_conn.execute("SELECT * FROM companies WHERE name = 'Acme Corp'").fetchone()
    assert row["board_url"] == "https://boards.greenhouse.io/acme"
    assert row["priority_tier"] == "A"
    assert row["founded_year"] == 2015

    count = db_conn.execute("SELECT COUNT(*) AS c FROM companies").fetchone()["c"]
    assert count == 2


def test_seed_companies_upserts_on_rerun(db_conn, seed_file, tmp_path):
    seed_companies(db_conn, seed_file)

    updated_yaml = tmp_path / "seed_companies_v2.yaml"
    updated_yaml.write_text(
        SEED_YAML.replace('priority_tier: "A"', 'priority_tier: "S"'),
        encoding="utf-8",
    )
    result = seed_companies(db_conn, updated_yaml)

    assert result == {"inserted": 0, "updated": 2}
    count = db_conn.execute("SELECT COUNT(*) AS c FROM companies").fetchone()["c"]
    assert count == 2

    row = db_conn.execute("SELECT priority_tier FROM companies WHERE name = 'Acme Corp'").fetchone()
    assert row["priority_tier"] == "S"


def test_seed_companies_rerun_does_not_wipe_fields_this_file_never_sets(db_conn, seed_file):
    # Real live bug: re-running seed_companies (e.g. to add one new company)
    # blindly overwrote employee_count/company_type/etc. back to NULL for
    # every already-existing company, since this yaml file never sets those
    # fields at all -- they're filled in later by app.enrichment, a
    # completely separate process. Confirmed live: wiped real FMP/StartupHub
    # data for Boomi, Workato, n8n, and Twilio.
    seed_companies(db_conn, seed_file)
    db_conn.execute(
        "UPDATE companies SET employee_count = 500, company_type = 'private' WHERE name = 'Acme Corp'"
    )
    db_conn.commit()

    result = seed_companies(db_conn, seed_file)  # re-run with the exact same, unchanged yaml
    assert result == {"inserted": 0, "updated": 2}

    row = db_conn.execute("SELECT employee_count, company_type FROM companies WHERE name = 'Acme Corp'").fetchone()
    assert row["employee_count"] == 500
    assert row["company_type"] == "private"


def test_seed_companies_explicit_yaml_value_still_overrides(db_conn, seed_file, tmp_path):
    # The COALESCE fix must only protect fields this file leaves unset --
    # a field the yaml DOES specify (e.g. priority_tier) must still win, as
    # test_seed_companies_upserts_on_rerun above already covers for a plain
    # change. This confirms it also wins over a value written by some other
    # process in between (not just the yaml's own prior value).
    seed_companies(db_conn, seed_file)
    db_conn.execute("UPDATE companies SET priority_tier = 'C' WHERE name = 'Acme Corp'")
    db_conn.commit()

    result = seed_companies(db_conn, seed_file)  # yaml still says "A"
    assert result == {"inserted": 0, "updated": 2}

    row = db_conn.execute("SELECT priority_tier FROM companies WHERE name = 'Acme Corp'").fetchone()
    assert row["priority_tier"] == "A"
