import pytest

from app.seed_filters import load_seed_filters, seed_filters

SEED_YAML = """
role_keyword_include:
  - "Solutions Architect"
  - "Sales Engineer"
tech_keyword_include:
  - "Kafka"
title_exclude:
  - "Intern"
seniority:
  - "mid"
  - "senior"
remote_type:
  - "remote"
location_include: []
industries_include: []
fit_score_threshold: 60
require_visa_sponsorship: false
posted_within_days: 30
daily_token_budget: 1.00
founded_after_year: null
"""


@pytest.fixture
def seed_file(tmp_path):
    path = tmp_path / "seed_filters.yaml"
    path.write_text(SEED_YAML, encoding="utf-8")
    return path


def test_load_seed_filters_splits_keywords_and_settings(seed_file):
    keywords, settings = load_seed_filters(seed_file)
    assert keywords["role_keyword_include"] == ["Solutions Architect", "Sales Engineer"]
    assert keywords["location_include"] == []
    assert settings["fit_score_threshold"] == 60
    assert settings["require_visa_sponsorship"] is False
    assert settings["founded_after_year"] is None


def test_seed_filters_inserts_keywords_and_settings(db_conn, seed_file):
    result = seed_filters(db_conn, seed_file)
    assert result["keywords_inserted"] == 7  # 2 role + 1 tech + 1 title_exclude + 2 seniority + 1 remote_type

    rows = db_conn.execute(
        "SELECT category, keyword FROM filter_keywords ORDER BY category, keyword"
    ).fetchall()
    pairs = {(r["category"], r["keyword"]) for r in rows}
    assert ("role_keyword_include", "Solutions Architect") in pairs
    assert ("tech_keyword_include", "Kafka") in pairs
    assert ("title_exclude", "Intern") in pairs

    settings_row = db_conn.execute(
        "SELECT value FROM filter_settings WHERE key = 'fit_score_threshold'"
    ).fetchone()
    assert settings_row["value"] == "60"

    bool_row = db_conn.execute(
        "SELECT value FROM filter_settings WHERE key = 'require_visa_sponsorship'"
    ).fetchone()
    assert bool_row["value"] == "false"


def test_seed_filters_is_idempotent_for_keywords(db_conn, seed_file):
    seed_filters(db_conn, seed_file)
    second = seed_filters(db_conn, seed_file)
    assert second["keywords_inserted"] == 0

    count = db_conn.execute("SELECT COUNT(*) AS c FROM filter_keywords").fetchone()["c"]
    assert count == 7  # unchanged after rerun


def test_seed_filters_settings_upsert_updates_value(db_conn, seed_file, tmp_path):
    seed_filters(db_conn, seed_file)

    updated = tmp_path / "seed_filters_v2.yaml"
    updated.write_text(SEED_YAML.replace("fit_score_threshold: 60", "fit_score_threshold: 75"), encoding="utf-8")
    seed_filters(db_conn, updated)

    row = db_conn.execute(
        "SELECT value FROM filter_settings WHERE key = 'fit_score_threshold'"
    ).fetchone()
    assert row["value"] == "75"
