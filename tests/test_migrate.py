from app.db import get_connection
from app.migrate import run_migrations

EXPECTED_TABLES = {
    "companies",
    "jobs",
    "fit_scores",
    "resume_feedback",
    "filter_settings",
    "filter_keywords",
    "workflow_runs",
    "step_logs",
    "schema_migrations",
}


def _table_names(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row["name"] for row in rows}


def test_run_migrations_creates_all_tables(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    try:
        run_migrations(conn)
        assert EXPECTED_TABLES.issubset(_table_names(conn))
    finally:
        conn.close()


def test_run_migrations_is_idempotent(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    try:
        first = run_migrations(conn)
        second = run_migrations(conn)
        assert first == [
            "0001_initial.sql", "0002_add_jobs_source_type.sql", "0003_add_adzuna_salary_estimate.sql",
            "0004_add_startuphub_last_checked.sql", "0005_add_link_checked_at.sql",
            "0006_add_salary_checked_at.sql", "0007_add_location_state.sql",
            "0008_add_cloud_platforms.sql", "0009_add_cloud_platforms_checked_at.sql",
            "0010_add_dol_lca_columns.sql", "0011_add_tinyfish_industry_columns.sql",
        ]
        assert second == []
    finally:
        conn.close()


def test_wal_mode_enabled(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_jobs_url_is_unique(db_conn):
    db_conn.execute(
        "INSERT INTO jobs (title, url) VALUES ('Solutions Architect', 'https://example.com/job/1')"
    )
    db_conn.commit()
    import sqlite3

    try:
        db_conn.execute(
            "INSERT INTO jobs (title, url) VALUES ('Other Title', 'https://example.com/job/1')"
        )
        db_conn.commit()
        assert False, "expected UNIQUE constraint violation"
    except sqlite3.IntegrityError:
        pass


def test_filter_keywords_unique_per_category(db_conn):
    db_conn.execute(
        "INSERT INTO filter_keywords (category, keyword) VALUES ('role_keyword_include', 'Solutions Architect')"
    )
    db_conn.commit()
    import sqlite3

    try:
        db_conn.execute(
            "INSERT INTO filter_keywords (category, keyword) VALUES ('role_keyword_include', 'Solutions Architect')"
        )
        db_conn.commit()
        assert False, "expected UNIQUE constraint violation"
    except sqlite3.IntegrityError:
        pass
