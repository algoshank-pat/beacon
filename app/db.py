"""SQLite connection helper.

WAL mode is required: ad hoc CLI commands (filter, visa-scan, fit-score,
enrich-companies, etc.) get run manually and independently, sometimes
overlapping with each other or with the scheduler process, and WAL mode
lets a reader and a writer coexist without "database is locked" errors
(SQLite's default rollback-journal mode is prone to those under concurrent
access on Windows).

WAL only helps readers-vs-a-writer, though -- two WRITERS (e.g. `filter`
and `enrich-companies` run in separate terminals at the same time, a real
scenario that's happened) still contend for SQLite's single write lock. A
busy_timeout makes SQLite retry/wait instead of raising immediately, which
is the standard fix -- without it, the second writer's very first INSERT
fails outright with "database is locked" rather than just waiting its turn.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import get_settings

_BUSY_TIMEOUT_MS = 30_000


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else get_settings().database_path
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    return conn
