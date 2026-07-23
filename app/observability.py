"""workflow_runs / step_logs helpers — every ingestion/filter/score/notify/decision
run and step gets recorded here for the `last-run` CLI and future debugging."""
from __future__ import annotations

import sqlite3


def start_workflow_run(conn: sqlite3.Connection, run_type: str) -> int:
    cursor = conn.execute(
        "INSERT INTO workflow_runs (run_type, status) VALUES (?, 'running')",
        (run_type,),
    )
    conn.commit()
    return cursor.lastrowid


def finish_workflow_run(conn: sqlite3.Connection, run_id: int, *, status: str, **counters) -> None:
    set_clause = ", ".join(f"{key} = ?" for key in counters)
    sql = "UPDATE workflow_runs SET status = ?, finished_at = CURRENT_TIMESTAMP"
    if set_clause:
        sql += f", {set_clause}"
    sql += " WHERE id = ?"
    conn.execute(sql, (status, *counters.values(), run_id))
    conn.commit()


def log_step(
    conn: sqlite3.Connection,
    *,
    workflow_run_id: int,
    step_name: str,
    step_status: str,
    job_id: int | None = None,
    detail: str | None = None,
    tokens_input: int = 0,
    tokens_output: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO step_logs
            (workflow_run_id, job_id, step_name, step_status, detail, tokens_input, tokens_output)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (workflow_run_id, job_id, step_name, step_status, detail, tokens_input, tokens_output),
    )
    conn.commit()
