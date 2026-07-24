"""Persistent scheduler process. Keeps running, firing four independent jobs:

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

from app.dates import CENTRAL
from app.db import get_connection
from app.filter_settings import get_filter_settings
from app.pipeline import (
    run_full_pipeline,
    run_scheduled_approval_poll,
    run_scheduled_enrichment,
    run_scheduled_fit_scoring,
)

LOG_PATH = Path(__file__).resolve().parent.parent / "scheduler.log"
LOCK_PATH = Path(__file__).resolve().parent.parent / "scheduler.lock"

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


def _approval_poll_interval_minutes() -> int:
    """Read once at startup -- like the main pipeline's fixed cron hours,
    this doesn't hot-reload mid-process; edit filter_settings and restart
    the scheduler to change it."""
    conn = get_connection()
    try:
        return get_filter_settings(conn).get("approval_poll_interval_minutes") or 30
    finally:
        conn.close()


def main() -> None:
    try:
        lock_file = acquire_single_instance_lock()
    except SchedulerAlreadyRunningError as exc:
        logger.error(str(exc))
        sys.exit(1)

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
