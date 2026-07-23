"""Automatic pipeline orchestration.

Three independent schedules, run separately (not chained together):

- run_full_pipeline(): seed-via-sheet -> ingest -> filter -> visa-scan ->
  link-check -> salary-refresh -> sort+resync, 3x/day (8am/1pm/6pm).
  Seed-via-sheet runs first so a company onboarded that way has its postings
  flow in during the very same run's ingest step, rather than waiting for
  the next one. A job lands on Beacon the moment it clears the Filter
  Engine -- no waiting on a score. Visa-scan updates that row's Visa Flag in
  place, and evicts it (moves to the Job Log instead) if it turns out
  visa-restricted. Link-check re-verifies jobs app.ingest.detect_closed_jobs
  can't cover (Adzuna-sourced, the majority of Beacon -- see app.link_check
  module docstring), evicting any confirmed closed. Salary-refresh
  re-attempts salary extraction via the full posting page for jobs still
  missing a real salary after the cheap description-only extraction at
  add-time (see app.salary_refresh module docstring). Sort+resync runs last
  so it captures link-check's evictions too.
- run_scheduled_fit_scoring(): fit-scoring, 3x/day alongside the main
  pipeline, kept as its own scheduled job so a fit-scoring failure/slowdown
  never blocks ingest/filter/visa-scan. Only scores jobs the user has
  explicitly flagged "Go Score" on Beacon, so volume is bounded by manual
  review rather than postings volume -- no longer needs isolating overnight
  for a fresh token budget. Also evicts any job flagged "Reject". Updates
  the Beacon row's score in place if above threshold, evicts it (moves to
  the Job Log) if below.
- run_scheduled_enrichment(): company enrichment, 3x/day alongside the main
  pipeline (offset 10 minutes so its Sheets writes don't compete with the
  other two schedules' for quota). Runs two independent passes each time
  (see app.enrichment module docstring): StartupHub.ai uncapped against the
  whole backlog (no published quota), and FMP capped by cumulative daily
  usage against `daily_enrichment_limit` (FMP's confirmed 250-requests/day
  free-tier limit is the only real quota constraint here). Used to be fully
  manual, then a single combined once/day pass (`python -m app.cli
  enrich-companies` still exists for on-demand combined runs).

Each step keeps its own connection and workflow_run, exactly as when run
individually via the CLI -- this module only chains/schedules them. A step
that raises is caught and logged so one bad step (e.g. a transient API
error) doesn't prevent the rest of a run from continuing -- important for
unattended/scheduled execution where nobody is there to notice a crash and
re-run manually.
"""
from __future__ import annotations

from app.approval import ApprovalPollSheetsError, count_consecutive_failures, run_approval_poll
from app.config import Settings, get_settings
from app.db import get_connection
from app.enrichment import get_fmp_enriched_today_count, run_fmp_enrichment, run_startuphub_enrichment
from app.filter_engine import run_filter_engine
from app.filter_settings import get_filter_settings
from app.fit_scoring import run_fit_scoring
from app.ingest import run_ingestion
from app.job_log import resolve_job_log_worksheet
from app.observability import finish_workflow_run, log_step, start_workflow_run
from app.resume import ResumeNotFoundError, get_base_resume_text
from app.seed_via_sheet import run_seed_via_sheet
from app.sheets import resolve_main_worksheet, resync_sheet_row_numbers, sort_and_resync_main_sheet


def run_full_pipeline(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    results: dict[str, dict] = {}
    errors: dict[str, str] = {}

    conn = get_connection()
    try:
        filter_settings = get_filter_settings(conn)
        job_log_ws = resolve_job_log_worksheet(settings, filter_settings)
    finally:
        conn.close()
    main_ws = resolve_main_worksheet(settings)

    # --- seed-via-sheet ---
    try:
        conn = get_connection()
        try:
            run_id = start_workflow_run(conn, "main_pipeline")
            result = run_seed_via_sheet(conn, main_ws, workflow_run_id=run_id)
            finish_workflow_run(conn, run_id, status="completed")
            results["seed_via_sheet"] = result
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        errors["seed_via_sheet"] = str(exc)

    # --- ingest ---
    try:
        conn = get_connection()
        try:
            results["ingest"] = run_ingestion(conn, settings, main_ws=main_ws, job_log_ws=job_log_ws)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 -- one bad step must not kill the pipeline
        errors["ingest"] = str(exc)

    # --- filter ---
    try:
        conn = get_connection()
        try:
            run_id = start_workflow_run(conn, "main_pipeline")
            result = run_filter_engine(conn, workflow_run_id=run_id, job_log_ws=job_log_ws, main_ws=main_ws)
            finish_workflow_run(conn, run_id, status="completed", jobs_filtered_out=result["filtered_out"])
            results["filter"] = result
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        errors["filter"] = str(exc)

    # --- visa-scan ---
    if settings.anthropic_api_key:
        try:
            import anthropic

            from app.visa_scan import run_visa_scan

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            conn = get_connection()
            try:
                filter_settings = get_filter_settings(conn)
                run_id = start_workflow_run(conn, "main_pipeline")
                result = run_visa_scan(
                    conn, client, filter_settings, workflow_run_id=run_id,
                    job_log_ws=job_log_ws, main_ws=main_ws,
                )
                finish_workflow_run(
                    conn, run_id, status="completed",
                    tokens_used_input=result["tokens_input"], tokens_used_output=result["tokens_output"],
                    estimated_cost_usd=result["estimated_cost_usd"],
                )
                results["visa_scan"] = result
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            errors["visa_scan"] = str(exc)
    else:
        errors["visa_scan"] = "ANTHROPIC_API_KEY not set"

    # --- link check (Adzuna-sourced jobs app.ingest.detect_closed_jobs
    # can't cover -- see app.link_check) -- runs before sort + resync below
    # so that step's resync captures the post-eviction row positions ---
    if main_ws is not None:
        try:
            from app.link_check import run_link_check

            conn = get_connection()
            try:
                filter_settings = get_filter_settings(conn)
                run_id = start_workflow_run(conn, "main_pipeline")
                result = run_link_check(
                    conn, limit=filter_settings.get("link_check_batch_size", 200),
                    workflow_run_id=run_id, main_ws=main_ws, job_log_ws=job_log_ws,
                )
                finish_workflow_run(conn, run_id, status="completed")
                results["link_check"] = result
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            errors["link_check"] = str(exc)

    # --- salary refresh (jobs still missing a real salary after the cheap
    # description-only extraction at add-time -- see app.salary_refresh) ---
    if main_ws is not None:
        try:
            from app.salary_refresh import run_salary_refresh

            conn = get_connection()
            try:
                filter_settings = get_filter_settings(conn)
                result = run_salary_refresh(
                    conn, limit=filter_settings.get("salary_refresh_batch_size", 200), main_ws=main_ws,
                )
                results["salary_refresh"] = result
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            errors["salary_refresh"] = str(exc)

    # --- cloud platforms refresh (jobs whose truncated description hid an
    # AWS/GCP/Azure mention -- see app.cloud_platforms_refresh) ---
    if main_ws is not None:
        try:
            from app.cloud_platforms_refresh import run_cloud_platforms_refresh

            conn = get_connection()
            try:
                filter_settings = get_filter_settings(conn)
                result = run_cloud_platforms_refresh(
                    conn, limit=filter_settings.get("cloud_platforms_refresh_batch_size", 200), main_ws=main_ws,
                )
                results["cloud_platforms_refresh"] = result
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            errors["cloud_platforms_refresh"] = str(exc)

    # --- sort + resync (per direct request, "sort alphabetically on
    # sheets at the end of the job") ---
    if main_ws is not None:
        try:
            conn = get_connection()
            try:
                results["sort_and_resync"] = sort_and_resync_main_sheet(conn, main_ws)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            errors["sort_and_resync"] = str(exc)

    return {"results": results, "errors": errors}


def run_scheduled_fit_scoring(settings: Settings | None = None) -> dict:
    """Fit-scores only jobs flagged via Beacon's "My Decision" column (see
    app.fit_scoring), throttled by the daily/monthly token budget. Also
    evicts any job with My Decision set to "Reject" every run. Runs 3x/day
    alongside the main pipeline; whatever doesn't fit in today's budget rolls
    over and gets picked up on a later run automatically."""
    settings = settings or get_settings()

    if not settings.anthropic_api_key:
        return {"error": "ANTHROPIC_API_KEY not set"}

    try:
        resume_text = get_base_resume_text()
    except ResumeNotFoundError as exc:
        return {"error": str(exc)}

    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    conn = get_connection()
    try:
        filter_settings = get_filter_settings(conn)
        job_log_ws = resolve_job_log_worksheet(settings, filter_settings)
        main_ws = resolve_main_worksheet(settings)
        run_id = start_workflow_run(conn, "main_pipeline")
        result = run_fit_scoring(
            conn, client, resume_text, filter_settings, workflow_run_id=run_id,
            job_log_ws=job_log_ws, main_ws=main_ws,
        )
        finish_workflow_run(
            conn, run_id, status="completed",
            jobs_scored=result["scored"],
            tokens_used_input=result["tokens_input"], tokens_used_output=result["tokens_output"],
            estimated_cost_usd=result["estimated_cost_usd"],
        )

        # Resync only, no sort -- fit-scoring evicts rows (sub-threshold
        # score, manual Reject) via app.sheets.remove_main_row, which shifts
        # every row below the deletion. Without this, jobs.sheet_row_number
        # drifts out of sync for app.approval's read_decision_row/
        # update_reminder_sent_at, which read/write that stored value
        # directly rather than re-locating the row like every other Sheets
        # write does (see resync_sheet_row_numbers). The full sort itself
        # only runs once per main-pipeline cycle, not here -- see
        # run_full_pipeline.
        if main_ws is not None:
            try:
                resync_sheet_row_numbers(conn, main_ws)
            except Exception as exc:  # noqa: BLE001 -- best-effort; next main-pipeline cycle's sort+resync will fully correct it regardless
                log_step(
                    conn, workflow_run_id=run_id, step_name="resync_sheet_row_numbers",
                    step_status="failed", detail=str(exc),
                )

        return result
    finally:
        conn.close()


def run_scheduled_enrichment(settings: Settings | None = None) -> dict:
    """Runs company enrichment's two independent, differently-paced passes
    (see app.enrichment module docstring) every time this job fires:

    1. StartupHub.ai -- uncapped, against every company never checked
       against it. No published rate limit, so no reason to throttle it;
       runs against the full backlog each time.
    2. FMP -- the only source for employee_count/company_type/funding_stage/
       revenue_or_valuation, and the only one with a confirmed real quota
       (250 requests/day free tier). Capped by cumulative usage across ALL
       of today's runs, not a flat per-run cap: `remaining =
       daily_enrichment_limit - get_fmp_enriched_today_count(conn)` (same
       pattern app.budget uses for the daily token budget) is what makes it
       safe to fire this job any number of times per day -- each invocation
       only claims whatever's left of today's FMP budget.

    Both always free, no LLM fallback. Runs at its own offset time relative
    to the main pipeline/fit-scoring so its Sheets writes (pushing newly-
    enriched fields onto existing Beacon rows) don't compete with theirs for
    quota at the same moment."""
    settings = settings or get_settings()

    conn = get_connection()
    try:
        filter_settings = get_filter_settings(conn)
        main_ws = resolve_main_worksheet(settings)
        run_id = start_workflow_run(conn, "main_pipeline")

        startuphub_result = run_startuphub_enrichment(
            conn, startuphub_api_key=settings.startuphub_api_key,
            workflow_run_id=run_id, main_ws=main_ws,
        )

        daily_limit = filter_settings.get("daily_enrichment_limit", 100)
        remaining = max(0, daily_limit - get_fmp_enriched_today_count(conn))
        if remaining > 0:
            fmp_result = run_fmp_enrichment(
                conn, fmp_api_key=settings.fmp_api_key, limit=remaining,
                workflow_run_id=run_id, main_ws=main_ws,
            )
            fmp_skipped = False
        else:
            fmp_result = {"evaluated": 0, "enriched_fmp": 0, "no_match_fmp": 0}
            fmp_skipped = True

        finish_workflow_run(conn, run_id, status="completed")
        return {
            "evaluated": startuphub_result["evaluated"] + fmp_result["evaluated"],
            "enriched": startuphub_result["enriched_startuphub"] + fmp_result["enriched_fmp"],
            "enriched_fmp": fmp_result["enriched_fmp"],
            "enriched_startuphub": startuphub_result["enriched_startuphub"],
            "no_match": startuphub_result["no_match_startuphub"] + fmp_result["no_match_fmp"],
            "fmp_skipped": fmp_skipped,
        }
    finally:
        conn.close()


def run_scheduled_approval_poll(settings: Settings | None = None) -> dict:
    """Reads the Decision column on every Beacon job still awaiting a
    decision, default every `approval_poll_interval_minutes` (30). Meant to
    run in the same process as the other two schedules -- a cheap Sheets
    read, not LLM-dependent. See app.approval for the per-job logic."""
    settings = settings or get_settings()

    conn = get_connection()
    try:
        filter_settings = get_filter_settings(conn)

        if filter_settings.get("pipeline_paused"):
            run_id = start_workflow_run(conn, "approval_poll")
            log_step(conn, workflow_run_id=run_id, step_name="decision_poll", step_status="skipped", detail="pipeline_paused is true")
            finish_workflow_run(conn, run_id, status="skipped")
            return {"skipped": True, "reason": "pipeline_paused"}

        ws = resolve_main_worksheet(settings)
        if ws is None:
            return {"error": "Google Sheets not configured"}

        run_id = start_workflow_run(conn, "approval_poll")
        try:
            result = run_approval_poll(conn, ws, filter_settings, workflow_run_id=run_id)
        except ApprovalPollSheetsError as exc:
            failures = count_consecutive_failures(conn, exclude_run_id=run_id) + 1
            max_failures = filter_settings.get("approval_poller_max_consecutive_failures") or 3
            if failures >= max_failures:
                log_step(
                    conn, workflow_run_id=run_id, step_name="decision_poll", step_status="CRITICAL",
                    detail=f"{failures} consecutive Approval Poller failures reaching Sheets API: {exc}",
                )
            finish_workflow_run(conn, run_id, status="failed", error_summary=str(exc))
            return {"error": str(exc)}

        finish_workflow_run(
            conn, run_id, status="completed",
            decisions_processed=result["approved"] + result["denied"],
        )
        return result
    finally:
        conn.close()
