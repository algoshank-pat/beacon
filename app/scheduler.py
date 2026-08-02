"""Persistent scheduler process. Keeps running, firing several independent jobs:

- the automatic pipeline (ingest -> filter -> visa-scan), 3x/day at 8am/1pm/6pm
- fit-scoring, separately, 5 minutes after each of those same three times --
  kept as its own job (not chained onto the pipeline) so a fit-scoring
  failure/slowdown never blocks ingest/filter/visa-scan, and staggered
  (not fired at the exact same instant) so the two jobs' Sheets API calls
  don't compete for quota at the same moment -- APScheduler's default
  thread-pool executor really can run same-instant cron jobs concurrently,
  and this is a real contributor to 429s hit live this session. Volume is
  naturally bounded by the user flagging jobs "Go Score" one at a time on
  Beacon, so running alongside the daytime pipeline (rather than overnight,
  as originally designed) no longer risks an unattended cost spike.
- company enrichment, same 3 daily trigger times as the main pipeline, offset
  10 minutes for the same Sheets-quota-contention reason as fit-scoring's
  offset above. Runs TWO independent passes each time it fires (see
  app.enrichment module docstring): StartupHub.ai, uncapped against the
  whole never-checked backlog (no published rate limit, so no quota reason
  to throttle it), and FMP, capped by *cumulative* usage across all of
  today's runs -- not each run individually -- via
  app.enrichment.get_fmp_enriched_today_count (same pattern app.budget uses
  for the daily LLM token budget) against `daily_enrichment_limit`, to
  respect FMP's confirmed 250-requests/day free-tier limit (the only one of
  the two sources with a real, confirmed quota). Used to be fully manual,
  then once/day as a single combined pass; moved to 3x/day once the
  daily-tracking fix made any frequency safe, then split into these two
  independently-paced passes so StartupHub's free volume stopped being
  needlessly throttled by FMP's scarce one.
- Job Log cleanup, weekly (Sunday midnight) -- deletes rows older than 60
  days to prevent unbounded growth (see app.job_log.cleanup_old_rows).
- health check, daily at 7:45am (before the main pipeline's first run) --
  logs a warning if visa-restricted jobs are stuck on Beacon or the Job Log
  has grown past a threshold (see app.health_check). Never fixes anything,
  only surfaces drift a human would otherwise have to notice by eyeballing
  row counts -- which is exactly how both live incidents above were found.
- nightly restart, daily at 3am -- spawns a fresh scheduler process and
  exits this one (see run_nightly_restart_job), so a code change committed
  during the day is running within 24 hours even if nobody manually
  restarts the process. Fixes a live gap: this process ran for days on
  code that predated several bug fixes, since nothing restarted it on
  deploy and there was no way to tell from the log alone that it was stale
  (see _git_commit_info, logged once at every startup for exactly that).

Windows Task Scheduler's only job is keeping THIS process alive (run at
startup, restart on failure) -- the actual scheduling logic lives here via
APScheduler, not in Task Scheduler itself, because Task Scheduler can't
cleanly express "if a fire time was missed because the machine was
off/asleep, run once as soon as possible instead of just skipping it" --
which matters on a personal laptop that isn't always on. That's what
misfire_grace_time + coalesce below are for, plus an immediate pipeline run
at process startup as a catch-up.

Run directly: `python -m app.scheduler`
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Conditional by platform, not a single cross-platform package -- both
# msvcrt (Windows) and fcntl (POSIX) are standard-library, so this needs no
# new dependency. Real users on macOS/Linux exist despite this project
# shipping Windows-first (its own developer machine); see
# acquire_single_instance_lock's docstring for why both locks share the
# same "auto-releases on crash, no stale file to clean up" property.
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.dates import CENTRAL
from app.db import get_connection
from app.filter_settings import get_filter_settings
from app.pipeline import (
    run_full_pipeline,
    run_scheduled_approval_poll,
    run_scheduled_enrichment,
    run_scheduled_fit_scoring,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = PROJECT_ROOT / "scheduler.log"
LOCK_PATH = PROJECT_ROOT / "scheduler.lock"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("scheduler")


class SchedulerAlreadyRunningError(Exception):
    """Raised when another scheduler instance already holds the lock."""


def acquire_single_instance_lock(lock_path: Path = LOCK_PATH):
    """Refuses to let a second scheduler instance run. Hit live more than
    once: a single `python -m app.scheduler` launch has resulted in two
    independent BlockingScheduler loops running concurrently against the
    same DB/Sheet -- overlapping pipeline runs, duplicate Sheets writes,
    duplicate notification emails. Root cause was never pinned down (it
    reproduced even from a single, deliberate, manually-typed launch), so
    this guards against it structurally instead.

    Uses an OS-level lock (msvcrt.locking on Windows, fcntl.flock on
    macOS/Linux), not a PID file -- both release automatically when the file
    handle closes, including on a crash or a forced kill (`Stop-Process
    -Force` / `kill -9`), so there's no stale lock file to manually clean up
    the way a PID-file approach would need on either platform.

    Returns the open file handle (keep a reference for the process's
    lifetime; closing it releases the lock). Raises
    SchedulerAlreadyRunningError if another instance already holds it.

    Deliberately never writes to the file (e.g. the holder's PID) --
    msvcrt's byte-range lock and the CRT's buffered I/O on the same handle
    don't mix safely; a write/flush through the very handle that holds the
    lock can itself raise PermissionError. Kept the same on the fcntl path
    too, for one identical contract on both platforms rather than two
    subtly different ones. The lock's existence is the only signal needed;
    nothing reads this file's contents.

    Not yet run live on macOS/Linux (this project's own use is Windows-only
    today) -- built directly against fcntl.flock's documented semantics
    (LOCK_EX | LOCK_NB raises BlockingIOError, a subclass of OSError, on
    contention -- the same exception type the existing msvcrt path already
    raises and handles below) rather than left as a real gap for whoever
    runs this on a Mac first."""
    lock_file = open(lock_path, "a+")
    try:
        if sys.platform == "win32":
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        raise SchedulerAlreadyRunningError(
            f"Another scheduler instance already holds the lock at {lock_path}"
        )
    return lock_file


def run_pipeline_job() -> None:
    logger.info("Pipeline run starting")
    try:
        outcome = run_full_pipeline()
        for step, result in outcome["results"].items():
            logger.info("  %s: %s", step, result)
        for step, error in outcome["errors"].items():
            logger.warning("  %s: ERROR (%s)", step, error)
    except Exception:
        logger.exception("Pipeline run crashed")
    logger.info("Pipeline run finished")


def run_fit_score_job() -> None:
    logger.info("Fit-score run starting")
    try:
        result = run_scheduled_fit_scoring()
        if "error" in result:
            logger.warning("  skipped: %s", result["error"])
        else:
            logger.info(
                "  scored %s/%s (%s above threshold, %s failed to parse, %s rejected, budget_exceeded=%s)",
                result["scored"], result["evaluated"], result["above_threshold"],
                result["failed"], result["rejected"], result["budget_exceeded"],
            )
    except Exception:
        logger.exception("Fit-score run crashed")
    logger.info("Fit-score run finished")


def run_enrich_companies_job() -> None:
    logger.info("Enrichment run starting")
    try:
        result = run_scheduled_enrichment()
        if "error" in result:
            logger.warning("  skipped: %s", result["error"])
        elif result.get("skipped"):
            logger.info("  skipped: %s", result["reason"])
        else:
            logger.info(
                "  evaluated %s (%s enriched: %s via FMP, %s via StartupHub; %s no match)",
                result["evaluated"], result["enriched"], result["enriched_fmp"],
                result["enriched_startuphub"], result["no_match"],
            )
    except Exception:
        logger.exception("Enrichment run crashed")
    logger.info("Enrichment run finished")


def run_approval_poll_job() -> None:
    logger.info("Approval poll starting")
    try:
        result = run_scheduled_approval_poll()
        if "error" in result:
            logger.warning("  error: %s", result["error"])
        elif result.get("skipped"):
            logger.info("  skipped: %s", result["reason"])
        else:
            logger.info(
                "  evaluated %s (approved %s, denied %s, reminders sent %s, still pending %s)",
                result["evaluated"], result["approved"], result["denied"],
                result["reminders_sent"], result["pending"],
            )
    except Exception:
        logger.exception("Approval poll crashed")
    logger.info("Approval poll finished")


def run_job_log_cleanup_job() -> None:
    """Weekly Job Log retention cleanup — deletes rows older than 60 days
    to prevent unbounded growth. The live incident that prompted this: 28k
    rows accumulated with no retention policy."""
    logger.info("Job Log cleanup starting")
    try:
        from app.job_log import cleanup_old_rows, resolve_job_log_worksheet

        settings = get_settings()
        conn = get_connection()
        try:
            filter_settings = get_filter_settings(conn)
        finally:
            conn.close()
        ws = resolve_job_log_worksheet(settings, filter_settings)

        if ws is None:
            logger.info("  skipped: Job Log sheet not configured")
            return

        result = cleanup_old_rows(ws, days_old=60)
        logger.info(
            "  deleted %s/%s rows (older than 60 days)",
            result["deleted"], result["evaluated"],
        )
    except Exception:
        logger.exception("Job Log cleanup crashed")
    logger.info("Job Log cleanup finished")


def run_health_check_job() -> None:
    """Daily sanity check for silent data-lifecycle drift (see
    app.health_check module docstring) -- just logs a warning per finding.
    Two live incidents prompted this: 443 visa-restricted jobs sat on
    Beacon unevicted, and the Job Log grew to 28k rows, both unnoticed for a
    while since neither failure raises an exception anywhere a human would
    see it."""
    logger.info("Health check starting")
    try:
        from app.health_check import JOB_LOG_ROW_WARNING_THRESHOLD, check_health
        from app.job_log import resolve_job_log_worksheet

        settings = get_settings()
        conn = get_connection()
        try:
            filter_settings = get_filter_settings(conn)
            job_log_ws = resolve_job_log_worksheet(settings, filter_settings)
            result = check_health(conn, job_log_ws=job_log_ws)
        finally:
            conn.close()

        stuck = result["stuck_restricted_on_beacon"]
        if stuck > 0:
            logger.warning(
                "  %s visa-restricted job(s) still on Beacon -- should have been evicted; "
                "check require_visa_sponsorship and app.visa_scan.evict_already_restricted_jobs", stuck,
            )
        else:
            logger.info("  0 visa-restricted jobs stuck on Beacon")

        row_count = result["job_log_row_count"]
        if row_count is None:
            logger.info("  Job Log row count: unavailable (not configured, or a Sheets outage)")
        elif row_count > JOB_LOG_ROW_WARNING_THRESHOLD:
            logger.warning(
                "  Job Log has %s rows (over the %s warning threshold) -- "
                "check run_job_log_cleanup_job is actually running",
                row_count, JOB_LOG_ROW_WARNING_THRESHOLD,
            )
        else:
            logger.info("  Job Log row count: %s", row_count)
    except Exception:
        logger.exception("Health check crashed")
    logger.info("Health check finished")


def run_nightly_restart_job() -> None:
    """Spawns a fresh scheduler process and exits this one -- the fix for
    the deploy-drift gap found live: a code change never takes effect in a
    long-running process until it restarts, and nothing was doing that
    automatically, so a scheduler ran for days on code that predated
    several bug fixes. Spawned detached so the new process survives after
    this one exits. This process's OS-level single-instance lock (see
    acquire_single_instance_lock) releases the moment this process's file
    handle closes -- including via os._exit below -- the same "auto-
    releases on crash/kill, no stale file to clean up" contract that lock
    already relies on, so the fresh process can acquire it right after."""
    logger.info("Nightly restart: spawning a fresh scheduler process (commit %s)", _git_commit_info())
    import subprocess
    import time

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        [sys.executable, "-m", "app.scheduler"], cwd=PROJECT_ROOT, creationflags=creationflags,
    )
    # Give the new process a moment to start up and attempt the lock before
    # this one releases it by exiting.
    time.sleep(2)
    logger.info("Nightly restart: new process spawned, exiting this one now")
    os._exit(0)  # skip cleanup/atexit -- the fresh child is already running


def _approval_poll_interval_minutes() -> int:
    """Read once at startup -- like the main pipeline's fixed cron hours,
    this doesn't hot-reload mid-process; edit filter_settings and restart
    the scheduler to change it."""
    conn = get_connection()
    try:
        return get_filter_settings(conn).get("approval_poll_interval_minutes") or 30
    finally:
        conn.close()


def _git_commit_info() -> str:
    """Short commit hash (+ "-dirty" if uncommitted changes) this running
    process was launched from, or "unknown" if `git` isn't on PATH / this
    isn't a git checkout. Logged once at startup so `scheduler.log` alone
    answers "is this process actually running today's code" -- a live gap
    found directly: a scheduler process kept running for days after several
    bug fixes had already been committed, because nothing restarts it
    automatically on a deploy, and there was no way to tell from the log
    alone that it was stale."""
    import subprocess

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip())
        return f"{commit}-dirty" if dirty else commit
    except Exception:  # noqa: BLE001 -- git absent/not a checkout must not block startup
        return "unknown"


def main() -> None:
    try:
        lock_file = acquire_single_instance_lock()
    except SchedulerAlreadyRunningError as exc:
        logger.error(str(exc))
        sys.exit(1)

    logger.info("Running code at commit %s", _git_commit_info())

    scheduler = BlockingScheduler(timezone=CENTRAL)

    # Main schedule: 8am / 1pm / 6pm Central. misfire_grace_time is generous
    # (6 hours) so a fire time missed while the machine was off/asleep still
    # runs once the process is up again, rather than being silently dropped.
    scheduler.add_job(
        run_pipeline_job,
        CronTrigger(hour="8,13,18", minute=0, timezone=CENTRAL),
        id="main_pipeline",
        misfire_grace_time=3600 * 6,
        coalesce=True,
    )

    # Fit-scoring: 5 minutes after each of the main pipeline's trigger times,
    # as a separate scheduled job -- volume is bounded by the user's manual
    # "Go Score" flags on Beacon, not by postings volume, so there's no need
    # to isolate it overnight for a fresh token budget anymore. The 5-minute
    # offset (not the exact same instant as main_pipeline) avoids both jobs'
    # Sheets API calls competing for quota at the same moment.
    scheduler.add_job(
        run_fit_score_job,
        CronTrigger(hour="8,13,18", minute=5, timezone=CENTRAL),
        id="fit_score",
        misfire_grace_time=3600 * 6,
        coalesce=True,
    )

    # Company enrichment: same 3 daily trigger times as the main pipeline,
    # offset 10 minutes so its Sheets writes (pushing newly-enriched fields
    # onto existing Beacon rows) don't compete with the main
    # pipeline's/fit-scoring's own Sheets activity for quota at the same
    # moment. Safe to fire this often (more, even) because the cumulative
    # total across ALL of today's runs is capped at `daily_enrichment_limit`,
    # not a flat per-run cap -- see app.pipeline.run_scheduled_enrichment.
    scheduler.add_job(
        run_enrich_companies_job,
        CronTrigger(hour="8,13,18", minute=10, timezone=CENTRAL),
        id="enrich_companies",
        misfire_grace_time=3600 * 6,
        coalesce=True,
    )

    # Approval Poller: cheap Sheets read, not LLM-dependent, so a shorter
    # independent cadence than the other two schedules is fine -- default
    # every 30 minutes via approval_poll_interval_minutes.
    scheduler.add_job(
        run_approval_poll_job,
        IntervalTrigger(minutes=_approval_poll_interval_minutes(), timezone=CENTRAL),
        id="approval_poll",
        misfire_grace_time=3600 * 6,
        coalesce=True,
    )

    # Job Log cleanup: weekly retention policy, runs Sunday at midnight. Deletes
    # rows older than 60 days to prevent unbounded growth (live incident: 28k
    # rows with no cleanup). Best-effort; a Sheets outage doesn't block the
    # rest of the scheduler.
    scheduler.add_job(
        run_job_log_cleanup_job,
        CronTrigger(day_of_week="6", hour=0, minute=0, timezone=CENTRAL),  # Sunday midnight
        id="job_log_cleanup",
        misfire_grace_time=3600 * 24,  # up to 24 hours grace if the process is down
        coalesce=True,
    )

    # Health check: daily, before the main pipeline's first run -- surfaces
    # the two kinds of silent drift a live incident hit (visa-restricted
    # jobs stuck on Beacon; Job Log growing unbounded) as a log warning
    # instead of waiting for someone to notice by eyeballing row counts.
    scheduler.add_job(
        run_health_check_job,
        CronTrigger(hour=7, minute=45, timezone=CENTRAL),
        id="health_check",
        misfire_grace_time=3600 * 6,
        coalesce=True,
    )

    # Nightly restart: fixes the deploy-drift gap a live incident hit --
    # this process kept running days-old code after several bug fixes had
    # already been committed, since nothing restarts it automatically.
    # 3am Central, clear of every other schedule here (8am/1pm/6pm pipeline,
    # 7:45am health check, Sunday-midnight Job Log cleanup).
    scheduler.add_job(
        run_nightly_restart_job,
        CronTrigger(hour=3, minute=0, timezone=CENTRAL),
        id="nightly_restart",
        misfire_grace_time=3600 * 6,
        coalesce=True,
    )

    # Catch-up: also run the pipeline once immediately when this process
    # starts, in case every scheduled fire time was missed (machine off for
    # a day, etc.).
    scheduler.add_job(run_pipeline_job, id="startup_catchup", next_run_time=datetime.now())

    logger.info("Scheduler starting. Log file: %s", LOG_PATH)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")
    finally:
        lock_file.close()  # releases the OS-level lock


if __name__ == "__main__":
    main()
