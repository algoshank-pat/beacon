"""Minimal SQL-file migration runner.

Migrations are numbered .sql files in app/migrations/, applied in filename
order inside a transaction each. Applied filenames are tracked in
schema_migrations so re-running only applies what's new.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


def applied_migrations(conn: sqlite3.Connection) -> set[str]:
    _ensure_migrations_table(conn)
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {row["filename"] for row in rows}


def pending_migrations(conn: sqlite3.Connection) -> list[Path]:
    already = applied_migrations(conn)
    all_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    return [f for f in all_files if f.name not in already]


def run_migrations(conn: sqlite3.Connection) -> list[str]:
    """Apply all pending migrations in order. Returns filenames applied."""
    applied = []
    for migration_file in pending_migrations(conn):
        sql = migration_file.read_text(encoding="utf-8")
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (filename) VALUES (?)",
            (migration_file.name,),
        )
        conn.commit()
        applied.append(migration_file.name)
    return applied
