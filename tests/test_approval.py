from datetime import datetime, timedelta, timezone

import pytest

from app.approval import ApprovalPollSheetsError, count_consecutive_failures, run_approval_poll
from app.observability import start_workflow_run
from app.sheets import MAIN_SHEET_COLUMNS
from tests.fakes import FakeWorksheet


def _iso(dt: datetime) -> str:
    # Matches SQLite's CURRENT_TIMESTAMP format (space-separated, naive UTC) --
    # what jobs.notified_at/first_seen_at actually contain in production,
    # and the only naive format app.dates.parse_datetime recognizes.
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _job(conn, **overrides):
    fields = {
        "title": "Solutions Architect",
        "url": "https://example.com/1",
        "status": "notified",
        "sheet_row_number": 2,
    }
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    cursor = conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", tuple(fields.values()))
    conn.commit()
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)).fetchone()


def _row(decision="", rejection_reason="", reminder_sent_at=""):
    row = [""] * len(MAIN_SHEET_COLUMNS)
    row[MAIN_SHEET_COLUMNS.index("Decision")] = decision
    row[MAIN_SHEET_COLUMNS.index("Rejection Reason")] = rejection_reason
    row[MAIN_SHEET_COLUMNS.index("Reminder Sent At")] = reminder_sent_at
    return row


def _ws(*rows):
    return FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, *rows])


def test_pending_within_window_takes_no_action(db_conn):
    recent = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
    job = _job(db_conn, notified_at=recent)
    ws = _ws(_row(decision="Pending"))

    result = run_approval_poll(db_conn, ws, {"approval_reminder_hours": 48})

    assert result == {"evaluated": 1, "pending": 1, "reminders_sent": 0, "approved": 0, "denied": 0}
    row = db_conn.execute("SELECT decision_processed_at FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["decision_processed_at"] is None


def test_pending_past_window_sends_reminder_once(db_conn):
    stale = _iso(datetime.now(timezone.utc) - timedelta(hours=72))
    _job(db_conn, notified_at=stale)
    ws = _ws(_row(decision="Pending"))

    result = run_approval_poll(db_conn, ws, {"approval_reminder_hours": 48})

    assert result["reminders_sent"] == 1
    row_dict = dict(zip(MAIN_SHEET_COLUMNS, ws.rows[1]))
    assert row_dict["Reminder Sent At"] != ""


def test_pending_past_window_but_reminder_already_sent_does_not_resend(db_conn):
    stale = _iso(datetime.now(timezone.utc) - timedelta(hours=72))
    _job(db_conn, notified_at=stale)
    ws = _ws(_row(decision="Pending", reminder_sent_at="07012026 08:00"))

    result = run_approval_poll(db_conn, ws, {"approval_reminder_hours": 48})

    assert result["reminders_sent"] == 0
    row_dict = dict(zip(MAIN_SHEET_COLUMNS, ws.rows[1]))
    assert row_dict["Reminder Sent At"] == "07012026 08:00"  # untouched


def test_pending_falls_back_to_first_seen_at_when_notified_at_missing(db_conn):
    # Jobs added to Beacon before notified_at started being set have it NULL --
    # the reminder window must still work off first_seen_at, not silently
    # never fire.
    stale = _iso(datetime.now(timezone.utc) - timedelta(hours=72))
    _job(db_conn, notified_at=None, first_seen_at=stale)
    ws = _ws(_row(decision="Pending"))

    result = run_approval_poll(db_conn, ws, {"approval_reminder_hours": 48})

    assert result["reminders_sent"] == 1


def test_deny_marks_rejected_and_records_reason(db_conn):
    job = _job(db_conn)
    ws = _ws(_row(decision="Deny", rejection_reason="Comp too low"))

    result = run_approval_poll(db_conn, ws, {"approval_reminder_hours": 48})

    assert result["denied"] == 1
    row = db_conn.execute("SELECT status, decision_processed_at FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["status"] == "rejected"
    assert row["decision_processed_at"] is not None


def test_deny_with_blank_reason_defaults_to_no_reason_given(db_conn):
    job = _job(db_conn)
    ws = _ws(_row(decision="Deny", rejection_reason=""))
    run_id = start_workflow_run(db_conn, "approval_poll")

    run_approval_poll(db_conn, ws, {"approval_reminder_hours": 48}, workflow_run_id=run_id)

    step = db_conn.execute(
        "SELECT detail FROM step_logs WHERE job_id = ? AND step_name = 'decision_poll'", (job["id"],)
    ).fetchone()
    assert step["detail"] == "no reason given"


def test_approve_marks_approved(db_conn):
    job = _job(db_conn)
    ws = _ws(_row(decision="Approve"))

    result = run_approval_poll(db_conn, ws, {"approval_reminder_hours": 48})

    assert result["approved"] == 1
    row = db_conn.execute("SELECT status, decision_processed_at FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["status"] == "approved"
    assert row["decision_processed_at"] is not None


def test_already_processed_jobs_are_excluded_from_the_query(db_conn):
    _job(db_conn, decision_processed_at=_iso(datetime.now(timezone.utc)), status="approved")
    ws = _ws()  # no row would be readable for this job -- proves it's never looked up

    result = run_approval_poll(db_conn, ws, {"approval_reminder_hours": 48})

    assert result["evaluated"] == 0


def test_jobs_without_a_sheet_row_are_excluded(db_conn):
    _job(db_conn, sheet_row_number=None)
    ws = _ws()

    result = run_approval_poll(db_conn, ws, {"approval_reminder_hours": 48})

    assert result["evaluated"] == 0


def test_sheets_read_failure_raises_and_preserves_prior_progress(db_conn):
    # Two jobs: first denies successfully (committed), second's row read blows
    # up -- the first job's processed state must survive, not get rolled back.
    job1 = _job(db_conn, sheet_row_number=2)
    job2 = _job(db_conn, url="https://example.com/2", sheet_row_number=3)
    ws = _ws(_row(decision="Deny"))  # only one data row -- row 3 read returns []

    class _BrokenWorksheet(FakeWorksheet):
        def row_values(self, row_number):
            if row_number == 3:
                raise RuntimeError("boom")
            return super().row_values(row_number)

    broken_ws = _BrokenWorksheet(rows=ws.rows)

    with pytest.raises(ApprovalPollSheetsError):
        run_approval_poll(db_conn, broken_ws, {"approval_reminder_hours": 48})

    row1 = db_conn.execute("SELECT status FROM jobs WHERE id = ?", (job1["id"],)).fetchone()
    assert row1["status"] == "rejected"


def test_count_consecutive_failures_stops_at_first_non_failed(db_conn):
    id1 = start_workflow_run(db_conn, "approval_poll")
    db_conn.execute("UPDATE workflow_runs SET status = 'completed' WHERE id = ?", (id1,))
    id2 = start_workflow_run(db_conn, "approval_poll")
    db_conn.execute("UPDATE workflow_runs SET status = 'failed' WHERE id = ?", (id2,))
    id3 = start_workflow_run(db_conn, "approval_poll")
    db_conn.execute("UPDATE workflow_runs SET status = 'failed' WHERE id = ?", (id3,))
    db_conn.commit()

    assert count_consecutive_failures(db_conn) == 2


def test_count_consecutive_failures_excludes_the_given_run_id(db_conn):
    id1 = start_workflow_run(db_conn, "approval_poll")
    db_conn.execute("UPDATE workflow_runs SET status = 'failed' WHERE id = ?", (id1,))
    current = start_workflow_run(db_conn, "approval_poll")  # still 'running' -- must not count as a break
    db_conn.commit()

    assert count_consecutive_failures(db_conn, exclude_run_id=current) == 1


def test_count_consecutive_failures_ignores_other_run_types(db_conn):
    id1 = start_workflow_run(db_conn, "main_pipeline")
    db_conn.execute("UPDATE workflow_runs SET status = 'failed' WHERE id = ?", (id1,))
    db_conn.commit()

    assert count_consecutive_failures(db_conn) == 0
