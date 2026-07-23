"""Company enrichment — employee count, HQ, public/private, funding stage,
revenue/valuation, industry. Two free sources, run as two INDEPENDENT
passes with independent "already checked" tracking (see migration 0004):

1. StartupHub.ai (`run_startuphub_enrichment`) -- an AI-startup directory,
   PARTIAL data (hq_location/founded_year/industry only on the current key's
   tier; employee_count/funding_stage/revenue_or_valuation require a "Pro
   Lite or higher" plan, confirmed via a live 403 on their /enrich endpoint).
   Has NO published rate limit, so this pass runs uncapped against every
   company that's never been checked against it -- there's no quota reason
   to throttle it.
2. Financial Modeling Prep (`run_fmp_enrichment`) -- free, structured,
   COMPLETE data, but only for PUBLICLY TRADED companies; their profile
   endpoint requires a resolvable stock ticker, so private companies simply
   return no match. This is the only source for employee_count/company_type/
   funding_stage/revenue_or_valuation, and the only one with a confirmed,
   real quota (250 requests/day) -- so it's the one callers should cap.

These used to be a single combined pass sharing one `financial_data_last_
checked` timestamp, with FMP tried first and StartupHub skipped entirely on
an FMP match. That coupled StartupHub's uncapped, free volume to FMP's
scarce quota for no reason, and also meant a company with an FMP match (a
public company) would never get StartupHub's founded_year/industry, since
FMP's own profile endpoint doesn't expose founded_year at all. Split per
direct request ("try searching for every company using startuphub.ai and
then leave the rest to fmp... since it's free, we can just hit this API
more times") once reordering alone was shown not to help: FMP still has to
be queried for every company to know if it's a public match, so call order
between the two sources never reduced FMP's call volume -- only decoupling
the two "checked" trackers does.

Enrichment must always be free, full stop -- there is deliberately no LLM
fallback tier here. An earlier version of this module filled whatever was
still missing after the two sources above via Sonnet 5 + server-side web
search (~$0.15-0.75/company, the most expensive per-unit cost anywhere in
this pipeline); removed per direct request ("if that info is not available,
do not spend tokens on it... I'll update them when I can"). If neither free
source has a field, it stays blank in the DB/Sheet for the user to fill in
by hand -- each source's own "checked" column still gets stamped either
way, so a company with no data from a source isn't re-queried against that
source on every future run.

Adzuna's API was checked and confirmed to expose nothing beyond a company
display_name -- no firmographic data at all -- so it isn't a source at all.
h1b_sponsor_last_5yrs/h1b_petitions_last_5yrs are NOT handled here: that's
specifically public DOL LCA disclosure data, a distinct dataset from what a
job posting's own text says (see app.visa_scan) -- a genuinely separate
future feature, not an extension of this module.
"""
from __future__ import annotations

import json
import sqlite3

from app.fmp import fetch_company_profile as fetch_fmp_profile
from app.observability import log_step
from app.sheets import update_company_columns
from app.startuphub import fetch_company_profile as fetch_startuphub_profile


def get_fmp_enriched_today_count(conn: sqlite3.Connection) -> int:
    """Companies whose financial_data_last_checked (FMP's own "checked"
    column) falls today (UTC, matching CURRENT_TIMESTAMP's own timezone --
    see _apply_enrichment). Used to track cumulative daily FMP usage against
    `daily_enrichment_limit` across however many times the scheduled job
    fires per day, the same pattern app.budget.get_spend_today uses for the
    LLM token budget -- this makes it safe to invoke the FMP pass any number
    of times in a day rather than tying the safety margin to a single fixed
    daily run. StartupHub's pass has no equivalent cap -- see module
    docstring."""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM companies WHERE date(financial_data_last_checked) = date('now')"
    ).fetchone()
    return row["c"]


def _apply_enrichment(conn: sqlite3.Connection, company_id: int, result: dict, checked_column: str) -> None:
    conn.execute(
        f"""
        UPDATE companies SET
            employee_count = COALESCE(?, employee_count),
            employee_count_range = COALESCE(?, employee_count_range),
            hq_location = COALESCE(?, hq_location),
            company_type = COALESCE(?, company_type),
            funding_stage = COALESCE(?, funding_stage),
            revenue_or_valuation = COALESCE(?, revenue_or_valuation),
            revenue_valuation_source = COALESCE(?, revenue_valuation_source),
            founded_year = COALESCE(?, founded_year),
            industry = COALESCE(?, industry),
            {checked_column} = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            result.get("employee_count"),
            result.get("employee_count_range"),
            result.get("hq_location"),
            result.get("company_type"),
            result.get("funding_stage"),
            result.get("revenue_or_valuation"),
            result.get("revenue_valuation_source"),
            result.get("founded_year"),
            result.get("industry"),
            company_id,
        ),
    )
    conn.commit()


def _push_to_beacon(conn: sqlite3.Connection, main_ws, company_id: int) -> None:
    """Re-reads the just-updated company row and pushes it onto every one of
    its jobs that's currently on Beacon. Without this, a company enriched
    after its job's Beacon row already exists would never show these fields
    there -- only a brand-new row created afterward picks them up."""
    if main_ws is None:
        return
    company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    job_ids = conn.execute(
        "SELECT id FROM jobs WHERE company_id = ? AND sheet_row_number IS NOT NULL", (company_id,)
    ).fetchall()
    for row in job_ids:
        update_company_columns(main_ws, row["id"], company)


def run_startuphub_enrichment(
    conn: sqlite3.Connection,
    startuphub_api_key: str | None = None,
    limit: int | None = None,
    workflow_run_id: int | None = None,
    main_ws=None,
) -> dict:
    """Enriches companies that (a) have never been checked against
    StartupHub (startuphub_last_checked IS NULL) and (b) have at least one
    job currently on Beacon. No `limit` by default -- StartupHub.ai has no
    published rate limit, so unlike the FMP pass there's no quota reason to
    throttle this one; it's meant to run against the entire backlog every
    time it fires. `limit` exists only for manual/on-demand control (e.g.
    `enrich-companies --limit N`), not for daily-quota management."""
    if not startuphub_api_key:
        return {"evaluated": 0, "enriched_startuphub": 0, "no_match_startuphub": 0}

    query = """
        SELECT DISTINCT c.* FROM companies c
        JOIN jobs j ON j.company_id = c.id
        WHERE c.startuphub_last_checked IS NULL
          AND j.sheet_row_number IS NOT NULL
    """
    if limit is not None:
        query += " LIMIT ?"
        companies = conn.execute(query, (limit,)).fetchall()
    else:
        companies = conn.execute(query).fetchall()

    enriched = no_match = 0
    for company in companies:
        try:
            result = fetch_startuphub_profile(company["name"], startuphub_api_key)
        except Exception as exc:  # network/API hiccup -- fall through
            result = None
            if workflow_run_id is not None:
                log_step(
                    conn, workflow_run_id=workflow_run_id, step_name="enrich_startuphub",
                    step_status="startuphub_failed", detail=f"{company['name']}: {exc}",
                )

        # Applied (and marked checked) even when empty -- avoids re-querying
        # StartupHub for the same company forever once it's confirmed to
        # have no data there.
        _apply_enrichment(conn, company["id"], result or {}, "startuphub_last_checked")
        try:
            _push_to_beacon(conn, main_ws, company["id"])
        except Exception as exc:  # a Sheets outage must not abort the whole backlog
            if workflow_run_id is not None:
                log_step(
                    conn, workflow_run_id=workflow_run_id, step_name="enrich_startuphub",
                    step_status="beacon_push_failed", detail=f"{company['name']}: {exc}",
                )
        if result:
            enriched += 1
        else:
            no_match += 1

        if workflow_run_id is not None:
            log_step(
                conn, workflow_run_id=workflow_run_id, step_name="enrich_startuphub",
                step_status="ok_startuphub" if result else "no_match",
                detail=f"{company['name']}: {json.dumps(result) if result else 'no data from StartupHub -- left blank'}",
            )

    return {"evaluated": len(companies), "enriched_startuphub": enriched, "no_match_startuphub": no_match}


def run_fmp_enrichment(
    conn: sqlite3.Connection,
    fmp_api_key: str | None = None,
    limit: int | None = None,
    workflow_run_id: int | None = None,
    main_ws=None,
) -> dict:
    """Enriches companies that (a) have never been checked against FMP
    (financial_data_last_checked IS NULL) and (b) have at least one job
    currently on Beacon. `limit` should be the caller's remaining
    daily_enrichment_limit headroom (see app.enrichment.get_fmp_enriched_
    today_count) -- this is the pass with a real, confirmed quota (FMP's
    free tier: 250 requests/day), so unlike the StartupHub pass, callers
    should always pass a limit for scheduled runs."""
    if not fmp_api_key:
        return {"evaluated": 0, "enriched_fmp": 0, "no_match_fmp": 0}

    query = """
        SELECT DISTINCT c.* FROM companies c
        JOIN jobs j ON j.company_id = c.id
        WHERE c.financial_data_last_checked IS NULL
          AND j.sheet_row_number IS NOT NULL
    """
    if limit is not None:
        query += " LIMIT ?"
        companies = conn.execute(query, (limit,)).fetchall()
    else:
        companies = conn.execute(query).fetchall()

    enriched = no_match = 0
    for company in companies:
        try:
            result = fetch_fmp_profile(company["name"], fmp_api_key)
        except Exception as exc:  # network/API hiccup -- fall through
            result = None
            if workflow_run_id is not None:
                log_step(
                    conn, workflow_run_id=workflow_run_id, step_name="enrich_fmp",
                    step_status="fmp_failed", detail=f"{company['name']}: {exc}",
                )

        # Applied (and marked checked) even when empty -- avoids re-querying
        # FMP for the same company forever once it's confirmed private/
        # unmatched.
        _apply_enrichment(conn, company["id"], result or {}, "financial_data_last_checked")
        try:
            _push_to_beacon(conn, main_ws, company["id"])
        except Exception as exc:  # a Sheets outage must not abort the whole backlog
            if workflow_run_id is not None:
                log_step(
                    conn, workflow_run_id=workflow_run_id, step_name="enrich_fmp",
                    step_status="beacon_push_failed", detail=f"{company['name']}: {exc}",
                )
        if result:
            enriched += 1
        else:
            no_match += 1

        if workflow_run_id is not None:
            log_step(
                conn, workflow_run_id=workflow_run_id, step_name="enrich_fmp",
                step_status="ok_fmp" if result else "no_match",
                detail=f"{company['name']}: {json.dumps(result) if result else 'no data from FMP -- left blank'}",
            )

    return {"evaluated": len(companies), "enriched_fmp": enriched, "no_match_fmp": no_match}


def run_enrichment(
    conn: sqlite3.Connection,
    settings: dict,
    fmp_api_key: str | None = None,
    startuphub_api_key: str | None = None,
    limit: int | None = None,
    workflow_run_id: int | None = None,
    main_ws=None,
) -> dict:
    """Manual/on-demand combined entry point (`enrich-companies` CLI) --
    runs both passes back-to-back with the same `limit` applied
    independently to each (None means uncapped for both, the CLI default).
    The scheduler does NOT use this: it calls run_startuphub_enrichment and
    run_fmp_enrichment directly with different limits (StartupHub uncapped,
    FMP capped at the day's remaining daily_enrichment_limit) -- see
    app.pipeline.run_scheduled_enrichment. `settings` is kept as a parameter
    for interface consistency with the other pipeline steps, though nothing
    here currently reads a budget from it."""
    startuphub_result = run_startuphub_enrichment(
        conn, startuphub_api_key=startuphub_api_key, limit=limit,
        workflow_run_id=workflow_run_id, main_ws=main_ws,
    )
    fmp_result = run_fmp_enrichment(
        conn, fmp_api_key=fmp_api_key, limit=limit,
        workflow_run_id=workflow_run_id, main_ws=main_ws,
    )

    return {
        "evaluated": startuphub_result["evaluated"] + fmp_result["evaluated"],
        "enriched": startuphub_result["enriched_startuphub"] + fmp_result["enriched_fmp"],
        "enriched_fmp": fmp_result["enriched_fmp"],
        "enriched_startuphub": startuphub_result["enriched_startuphub"],
        "no_match": startuphub_result["no_match_startuphub"] + fmp_result["no_match_fmp"],
    }
