"""Approval Poller (M6) — reads the Decision column on each job's Beacon row
and acts on it: Pending (with a once-only stalled reminder past
approval_reminder_hours), Deny (reject), or Approve (mark approved).

Scoped by `sheet_row_number IS NOT NULL AND decision_processed_at IS NULL` --
not `status = 'notified'`, which nothing sets anymore since the
Beacon-immediate-add redesign (see app.sheets). `decision_processed_at` is
the idempotency guard: once set, a job is never reprocessed even if its
Decision cell still shows Approve/Deny later.

The Claude Desktop resume/cover-letter handoff itself (clipboard copy,
`claude://` deep link, Handoff Prompt column write) is a separate milestone
(M7, not built yet) -- Approve here only records the decision. Once M7
exists, its handoff trigger slots in where noted below.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.dates import format_central, parse_datetime
from app.observability import log_step
from app.sheets import read_decision_row, update_reminder_sent_at

DECISION_APPROVE = "Approve"
DECISION_DENY = "Deny"


class ApprovalPollSheetsError(Exception):
    """Raised when the Decision cell can't be read/written after retries --
    treated as the whole poll cycle failing (auth error, network error,
    expired service account credentials), not a single job's problem."""


def count_consecutive_failures(conn: sqlite3.Connection, exclude_run_id: int | None = None) -> int:
    """Counts trailing consecutive failed `approval_poll` workflow_runs, most
    recent first, stopping at the first non-failed run. Used to decide when
    to escalate to a CRITICAL step_logs entry."""
    if exclude_run_id is None:
        rows = conn.execute(
            "SELECT status FROM workflow_runs WHERE run_type = 'approval_poll' ORDER BY id DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT status FROM workflow_runs WHERE run_type = 'approval_poll' AND id != ? ORDER BY id DESC",
            (exclude_run_id,),
        ).fetchall()
    count = 0
    for row in rows:
        if row["status"] != "failed":
            break
        count += 1
    return count


def _hours_since(value) -> float | None:
    dt = parse_datetime(value) if not isinstance(value, datetime) else value
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def run_approval_poll(
    conn: sqlite3.Connection,
    ws,
    filter_settings: dict,
    workflow_run_id: int | None = None,
) -> dict:
    jobs = conn.execute(
        "SELECT * FROM jobs WHERE sheet_row_number IS NOT NULL AND decision_processed_at IS NULL"
    ).fetchall()

    reminder_hours = filter_settings.get("approval_reminder_hours") or 48
    evaluated = pending = reminders_sent = approved = denied = 0

    for job in jobs:
        try:
            row = read_decision_row(ws, job["sheet_row_number"])
        except Exception as exc:  # noqa: BLE001 -- any failure here means Sheets is unreachable
            raise ApprovalPollSheetsError(str(exc)) from exc

        evaluated += 1
        decision = (row["decision"] or "").strip()

        if decision == DECISION_DENY:
            reason = row["rejection_reason"].strip() if row["rejection_reason"] else "no reason given"
            conn.execute(
                "UPDATE jobs SET status = 'rejected', decision_processed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (job["id"],),
            )
            conn.commit()
            denied += 1
            if workflow_run_id is not None:
                log_step(
                    conn, workflow_run_id=workflow_run_id, job_id=job["id"],
                    step_name="decision_poll", step_status="denied", detail=reason,
                )
            continue

        if decision == DECISION_APPROVE:
            conn.execute(
                "UPDATE jobs SET status = 'approved', decision_processed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (job["id"],),
            )
            conn.commit()
            approved += 1
            if workflow_run_id is not None:
                log_step(
                    conn, workflow_run_id=workflow_run_id, job_id=job["id"],
                    step_name="decision_poll", step_status="approved",
                    detail="Resume/cover-letter handoff not yet built (M7) -- decision recorded only.",
                )
            continue

        # Pending (or blank/unrecognized -- fail-open as Pending rather than
        # erroring on a dropdown cell that hasn't been touched yet).
        pending += 1
        baseline = job["notified_at"] or job["first_seen_at"]
        hours_pending = _hours_since(baseline)
        reminder_already_sent = bool(row["reminder_sent_at"])

        if hours_pending is not None and hours_pending >= reminder_hours and not reminder_already_sent:
            try:
                update_reminder_sent_at(ws, job["sheet_row_number"], format_central(datetime.now(timezone.utc)))
            except Exception as exc:  # noqa: BLE001
                raise ApprovalPollSheetsError(str(exc)) from exc
            reminders_sent += 1
            if workflow_run_id is not None:
                log_step(
                    conn, workflow_run_id=workflow_run_id, job_id=job["id"],
                    step_name="reminder", step_status="sent", detail=f"pending {hours_pending:.1f}h",
                )
        elif workflow_run_id is not None:
            log_step(
                conn, workflow_run_id=workflow_run_id, job_id=job["id"],
                step_name="decision_poll", step_status="pending", detail="still pending, no action",
            )

    return {
        "evaluated": evaluated,
        "pending": pending,
        "reminders_sent": reminders_sent,
        "approved": approved,
        "denied": denied,
    }
