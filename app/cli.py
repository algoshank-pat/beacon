"""CLI entrypoint: python -m app.cli <command>"""
from __future__ import annotations

import click

from app.config import get_settings
from app.db import get_connection
from app.enrichment import run_enrichment
from app.filter_engine import run_filter_engine
from app.filter_settings import get_filter_settings
from app.fit_scoring import run_fit_scoring
from app.ingest import run_ingestion
from app.job_log import resolve_job_log_worksheet
from app.migrate import run_migrations
from app.observability import finish_workflow_run, start_workflow_run
from app.pipeline import run_full_pipeline, run_scheduled_approval_poll
from app.resume import ResumeNotFoundError, get_base_resume_text
from app.seed import seed_companies as seed_companies_fn
from app.seed_filters import seed_filters as seed_filters_fn
from app.seed_via_sheet import run_seed_via_sheet
from app.sheets import resolve_main_worksheet
from app.visa_scan import run_visa_scan


@click.group()
def cli() -> None:
    """Job search automation app CLI."""


@cli.command()
def migrate() -> None:
    """Apply any pending database migrations."""
    conn = get_connection()
    try:
        applied = run_migrations(conn)
    finally:
        conn.close()

    if applied:
        click.echo(f"Applied {len(applied)} migration(s):")
        for name in applied:
            click.echo(f"  - {name}")
    else:
        click.echo("No pending migrations.")


@cli.command(name="seed-companies")
@click.option(
    "--file",
    "seed_file",
    default="seed_companies.yaml",
    show_default=True,
    help="Path to the seed companies YAML file.",
)
def seed_companies_cmd(seed_file: str) -> None:
    """Upsert companies from a YAML seed file into the companies table."""
    conn = get_connection()
    try:
        result = seed_companies_fn(conn, seed_file)
    finally:
        conn.close()
    click.echo(f"Seeded companies: {result['inserted']} inserted, {result['updated']} updated.")


@cli.command()
def ingest() -> None:
    """Run Adzuna + targeted (Greenhouse/Lever/Ashby) ingestion once.

    Interim command for M2 — folded into the full `run-pipeline` command once
    filtering/scoring exist (M3/M4).
    """
    conn = get_connection()
    try:
        result = run_ingestion(conn, get_settings())
    finally:
        conn.close()

    click.echo(f"Workflow run #{result['workflow_run_id']}")

    adzuna = result["adzuna"]
    if adzuna.get("skipped"):
        click.echo(f"  Adzuna: skipped ({adzuna['reason']})")
    else:
        click.echo(
            f"  Adzuna: {adzuna['keywords_queried']} keyword(s) queried, "
            f"{adzuna['inserted']} inserted, {adzuna['duplicate_url']} dup-url, "
            f"{adzuna['duplicate_fuzzy']} dup-fuzzy, {adzuna['failed_keywords']} failed"
        )

    for company_name, r in result["targeted"].items():
        if r.get("skipped"):
            click.echo(f"  {company_name}: skipped ({r['reason']})")
        elif r.get("error"):
            click.echo(f"  {company_name}: ERROR ({r['error']})")
        else:
            click.echo(
                f"  {company_name}: fetched={r['fetched']} inserted={r['inserted']} "
                f"dup-url={r['duplicate_url']} dup-fuzzy={r['duplicate_fuzzy']} closed={r['closed']}"
            )

    click.echo(f"Total inserted: {result['total_inserted']}")


@cli.command(name="seed-filters")
@click.option(
    "--file",
    "seed_file",
    default="seed_filters.yaml",
    show_default=True,
    help="Path to the seed filter criteria YAML file.",
)
def seed_filters_cmd(seed_file: str) -> None:
    """One-time import of seed_filters.yaml into filter_settings/filter_keywords."""
    conn = get_connection()
    try:
        result = seed_filters_fn(conn, seed_file)
    finally:
        conn.close()
    click.echo(
        f"Seeded filters: {result['keywords_inserted']} keyword(s) inserted, "
        f"{result['settings_upserted']} setting(s) upserted."
    )


@cli.command(name="seed-via-sheet")
def seed_via_sheet_cmd() -> None:
    """Onboards companies flagged via Job ID="SEED" rows on Beacon (guesses
    and verifies a Greenhouse/Lever/Ashby board), and cleans up rows already
    processed on a prior run."""
    settings = get_settings()
    conn = get_connection()
    try:
        main_ws = resolve_main_worksheet(settings)
        run_id = start_workflow_run(conn, "main_pipeline")
        result = run_seed_via_sheet(conn, main_ws, workflow_run_id=run_id)
        finish_workflow_run(conn, run_id, status="completed")
    finally:
        conn.close()
    click.echo(
        f"Processed {result['processed']} seed row(s): {result['added']} added, "
        f"{result['not_found']} not found. Cleaned up {result['cleaned_up']} already-processed row(s)."
    )


@cli.command(name="filter")
def filter_cmd() -> None:
    """Run the Filter Engine once over jobs at status='new'. Passing jobs
    are added to Beacon immediately (no waiting on visa-scan/fit-score)."""
    settings = get_settings()
    conn = get_connection()
    try:
        filter_settings = get_filter_settings(conn)
        job_log_ws = resolve_job_log_worksheet(settings, filter_settings)
        main_ws = resolve_main_worksheet(settings)
        run_id = start_workflow_run(conn, "main_pipeline")
        result = run_filter_engine(conn, workflow_run_id=run_id, job_log_ws=job_log_ws, main_ws=main_ws)
        finish_workflow_run(
            conn, run_id, status="completed",
            jobs_filtered_out=result["filtered_out"],
        )
    finally:
        conn.close()
    click.echo(
        f"Evaluated {result['evaluated']} job(s): "
        f"{result['passed']} passed, {result['filtered_out']} filtered out."
    )


@cli.command(name="visa-scan")
@click.option("--limit", type=int, default=None, help="Max number of jobs to scan this run.")
def visa_scan_cmd(limit: int | None) -> None:
    """Run the Visa Scanner (regex + Haiku fallback) over jobs at status='new'."""
    import anthropic

    settings = get_settings()
    if not settings.anthropic_api_key:
        raise click.ClickException("ANTHROPIC_API_KEY is not set in .env")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    conn = get_connection()
    try:
        filter_settings = get_filter_settings(conn)
        job_log_ws = resolve_job_log_worksheet(settings, filter_settings)
        main_ws = resolve_main_worksheet(settings)
        run_id = start_workflow_run(conn, "main_pipeline")
        result = run_visa_scan(
            conn, client, filter_settings, limit=limit, workflow_run_id=run_id,
            job_log_ws=job_log_ws, main_ws=main_ws,
        )
        finish_workflow_run(
            conn, run_id, status="completed",
            tokens_used_input=result["tokens_input"],
            tokens_used_output=result["tokens_output"],
            estimated_cost_usd=result["estimated_cost_usd"],
        )
    finally:
        conn.close()
    click.echo(
        f"Scanned {result['scanned']} job(s): {result['regex_hits']} regex hits, "
        f"{result['no_mention']} no-mention (free, no Haiku call), "
        f"{result['haiku_calls']} Haiku call(s) ({result['haiku_failures']} failed, will retry next run), "
        f"{result['restricted_filtered']} filtered as restricted. "
        f"Tokens: {result['tokens_input']} in / {result['tokens_output']} out."
    )


@cli.command(name="fit-score")
@click.option("--limit", type=int, default=None, help="Max number of jobs to score this run.")
def fit_score_cmd(limit: int | None) -> None:
    """Score jobs flagged via Beacon's "My Decision" column (set to "Go Score",
    or "AI Score Pending" if a prior run's API call failed and left it there
    for retry) against the base resume. Also evicts any job with My Decision
    set to "Reject". Does nothing if nothing is currently flagged."""
    import anthropic

    settings = get_settings()
    if not settings.anthropic_api_key:
        raise click.ClickException("ANTHROPIC_API_KEY is not set in .env")

    try:
        resume_text = get_base_resume_text()
    except ResumeNotFoundError as exc:
        raise click.ClickException(str(exc))

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    conn = get_connection()
    try:
        filter_settings = get_filter_settings(conn)
        job_log_ws = resolve_job_log_worksheet(settings, filter_settings)
        main_ws = resolve_main_worksheet(settings)
        run_id = start_workflow_run(conn, "main_pipeline")
        result = run_fit_scoring(
            conn, client, resume_text, filter_settings, limit=limit, workflow_run_id=run_id,
            job_log_ws=job_log_ws, main_ws=main_ws,
        )
        finish_workflow_run(
            conn, run_id, status="completed",
            jobs_scored=result["scored"],
            tokens_used_input=result["tokens_input"],
            tokens_used_output=result["tokens_output"],
            estimated_cost_usd=result["estimated_cost_usd"],
        )
    finally:
        conn.close()

    click.echo(
        f"Scored {result['scored']}/{result['evaluated']} job(s), "
        f"{result['above_threshold']} above threshold, {result['failed']} failed to parse. "
        f"Tokens: {result['tokens_input']} in / {result['tokens_output']} out."
    )
    if result["budget_exceeded"]:
        click.echo("WARNING: token budget exceeded -- scoring paused before all jobs were evaluated.")


@cli.command(name="enrich-companies")
@click.option("--limit", type=int, default=None, help="Max number of companies to enrich this run.")
def enrich_companies_cmd(limit: int | None) -> None:
    """Research employee count/HQ/public-private/funding stage/revenue for
    companies with active jobs that haven't been enriched yet. Always free
    (FMP + StartupHub.ai only) -- fields neither covers stay blank rather
    than falling back to an LLM call. Runs both sources' passes back-to-back
    with `--limit` applied independently to each (see app.enrichment module
    docstring for why they're two independent passes with two independent
    "checked" trackers) -- the scheduled job instead runs StartupHub
    uncapped and only caps FMP, since FMP is the one with a real quota."""
    settings = get_settings()
    conn = get_connection()
    try:
        filter_settings = get_filter_settings(conn)
        main_ws = resolve_main_worksheet(settings)
        run_id = start_workflow_run(conn, "main_pipeline")
        result = run_enrichment(
            conn, filter_settings,
            fmp_api_key=settings.fmp_api_key, startuphub_api_key=settings.startuphub_api_key,
            limit=limit, workflow_run_id=run_id, main_ws=main_ws,
        )
        finish_workflow_run(conn, run_id, status="completed")
    finally:
        conn.close()

    click.echo(
        f"Evaluated {result['evaluated']} company(s): {result['enriched']} enriched "
        f"({result['enriched_fmp']} via FMP, {result['enriched_startuphub']} via StartupHub), "
        f"{result['no_match']} had no data from either free source (left blank)."
    )


@cli.command(name="approval-poll")
def approval_poll_cmd() -> None:
    """Read the Decision column on every Beacon job awaiting a decision --
    Approve/Deny/stalled-reminder. Same logic the scheduler runs every
    approval_poll_interval_minutes; useful for testing without waiting."""
    result = run_scheduled_approval_poll()
    if "error" in result:
        raise click.ClickException(result["error"])
    if result.get("skipped"):
        click.echo(f"Skipped: {result['reason']}")
        return
    click.echo(
        f"Evaluated {result['evaluated']} job(s) awaiting a decision: "
        f"{result['approved']} approved, {result['denied']} denied, "
        f"{result['reminders_sent']} reminder(s) sent, {result['pending']} still pending."
    )


@cli.command()
def pipeline() -> None:
    """Run seed-via-sheet -> ingest -> filter -> visa-scan in one call. This
    is the automatic chain (also what the scheduler runs 3x/day) -- jobs
    land on Beacon the moment they pass filtering. Fit-scoring runs
    separately (its own schedule, `fit-score` manually) and updates or
    evicts the Beacon row once a score exists; enrich-companies stays
    manual too."""
    outcome = run_full_pipeline()

    for step, result in outcome["results"].items():
        click.echo(f"{step}: {result}")
    for step, error in outcome["errors"].items():
        click.echo(f"{step}: ERROR ({error})")


if __name__ == "__main__":
    cli()
