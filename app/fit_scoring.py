"""Fit Scoring — JD vs resume via the Claude API (Sonnet 5), budget-gated."""
from __future__ import annotations

import json
import sqlite3

from app.budget import BudgetTracker, estimate_cost_usd
from app.job_log import STAGE_SCORED_BELOW_THRESHOLD, STAGE_USER_REJECTED, upsert_job_log_row
from app.observability import log_step
from app.sheets import (
    MY_DECISION_AI_SCORE_PENDING,
    MY_DECISION_AI_SCORED,
    MY_DECISION_REJECT,
    get_rejected_job_ids,
    get_scoreable_job_ids,
    remove_main_row,
    update_my_decision,
    update_score,
)

SONNET_MODEL = "claude-sonnet-5"


class FitScoreParseError(Exception):
    def __init__(self, message: str, usage: dict | None = None):
        super().__init__(message)
        self.usage = usage or {"input_tokens": 0, "output_tokens": 0}

FIT_SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "Overall fit score, 0-100"},
        "matched_skills": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["score", "matched_skills", "gaps", "summary"],
    "additionalProperties": False,
}

FIT_SCORE_PROMPT = """Score how well this candidate's resume fits the job description below.

Be systematic: enumerate the JD's concrete requirements (skills, tools, years
of experience, domain knowledge) and check each one against specific evidence
in the resume, rather than forming a general impression. This keeps the score
grounded and reduces run-to-run variance on the same inputs.

Return:
- score: an integer 0-100 for overall fit
- matched_skills: skills/experience from the resume that directly match JD requirements
- gaps: JD requirements the resume doesn't clearly demonstrate
- summary: 1-2 sentence summary of the fit

## Job Description
Title: {title}
Company: {company}

{description}

## Candidate Resume
{resume}
"""


def score_job(
    client, job_title: str, company_name: str, description: str, resume_text: str
) -> tuple[dict, dict]:
    # Sonnet 5 rejects non-default temperature/top_p/top_k outright (400) --
    # unlike Haiku 4.5, it's in the model family where sampling params were
    # removed in favor of prompting/effort. Score-to-score variance is
    # controlled via the prompt instruction below instead.
    response = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=2048,
        output_config={"format": {"type": "json_schema", "schema": FIT_SCORE_SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": FIT_SCORE_PROMPT.format(
                    title=job_title,
                    company=company_name,
                    description=description[:12000],
                    resume=resume_text[:12000],
                ),
            }
        ],
    )
    text = next(block.text for block in response.content if block.type == "text")
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        # output_config.format only guarantees valid JSON if generation
        # completes normally -- a max_tokens cutoff mid-generation (real,
        # observed on a live JD) still produces truncated, invalid JSON.
        reason = getattr(response, "stop_reason", "unknown")
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        raise FitScoreParseError(
            f"Failed to parse fit-score JSON (stop_reason={reason}): {exc}", usage=usage
        ) from exc
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    return result, usage


def _get_company(conn: sqlite3.Connection, job: sqlite3.Row) -> sqlite3.Row | None:
    if job["company_id"] is None:
        return None
    return conn.execute("SELECT * FROM companies WHERE id = ?", (job["company_id"],)).fetchone()


def _process_rejections(
    conn: sqlite3.Connection, main_ws, job_log_ws, workflow_run_id: int | None,
) -> int:
    """Evicts every Beacon job whose My Decision is "Reject" -- independent
    of scoring entirely, checked every run alongside it since both read the
    same My Decision column."""
    rejected_job_ids = get_rejected_job_ids(main_ws)
    rejected = 0
    for job_id in rejected_job_ids:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            continue
        if remove_main_row(main_ws, job_id):
            conn.execute("UPDATE jobs SET sheet_row_number = NULL WHERE id = ?", (job_id,))
            conn.commit()
        if job_log_ws is not None:
            company = _get_company(conn, job)
            upsert_job_log_row(job_log_ws, job, company, STAGE_USER_REJECTED, my_decision=MY_DECISION_REJECT)
        rejected += 1
        if workflow_run_id is not None:
            log_step(
                conn, workflow_run_id=workflow_run_id, job_id=job_id,
                step_name="fit_score", step_status="rejected", detail="My Decision = Reject",
            )
    return rejected


def run_fit_scoring(
    conn: sqlite3.Connection,
    client,
    resume_text: str,
    settings: dict,
    limit: int | None = None,
    workflow_run_id: int | None = None,
    job_log_ws=None,
    main_ws=None,
) -> dict:
    """Scores only jobs the user has explicitly flagged via the Beacon
    sheet's My Decision column ("Go Score", or "AI Score Pending" if a prior
    run claimed it but failed) -- replaces the earlier "score everything on
    Beacon automatically" behavior, which got too expensive to run
    unattended once the tracked-company list (and postings volume) grew
    substantially. A flagged job also still needs status='new' with a
    visa_flag already set (i.e. it passed the Filter Engine and Visa
    Scanner) -- already-'scored' jobs are never re-scored here (that happens
    separately, post-resume-registration, in M7).

    Also processes My Decision = "Reject" every run (evicts to the Job Log),
    since that's the same column and there's no other scheduled step
    watching it.

    Requires main_ws (there's no other source of truth for what's flagged);
    returns an all-zero result without querying the DB at all if it's not
    configured or nothing is currently flagged/rejected, so this never
    spends money without an explicit per-job signal."""
    empty_result = {
        "evaluated": 0, "scored": 0, "above_threshold": 0, "failed": 0,
        "rejected": 0, "budget_exceeded": False, "tokens_input": 0,
        "tokens_output": 0, "estimated_cost_usd": 0.0,
    }
    if main_ws is None:
        return empty_result

    rejected = _process_rejections(conn, main_ws, job_log_ws, workflow_run_id)

    requested_job_ids = get_scoreable_job_ids(main_ws)
    if not requested_job_ids:
        result = dict(empty_result)
        result["rejected"] = rejected
        return result

    placeholders = ",".join("?" * len(requested_job_ids))
    query = f"""
        SELECT j.*, c.name AS company_name FROM jobs j
        LEFT JOIN companies c ON j.company_id = c.id
        WHERE j.status = 'new' AND j.visa_flag IS NOT NULL
          AND j.id IN ({placeholders})
    """
    params = list(requested_job_ids)
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    jobs = conn.execute(query, params).fetchall()

    tracker = BudgetTracker(conn, settings.get("daily_token_budget"), settings.get("monthly_token_budget"))
    threshold = settings.get("fit_score_threshold", 60)

    scored = above_threshold = failed = 0
    budget_exceeded = False
    total_input_tokens = total_output_tokens = 0
    total_cost = 0.0

    for job in jobs:
        if not tracker.has_budget():
            budget_exceeded = True
            if workflow_run_id is not None:
                log_step(
                    conn,
                    workflow_run_id=workflow_run_id,
                    step_name="fit_score",
                    step_status="CRITICAL",
                    detail=(
                        f"Token budget exceeded (daily remaining=${tracker.remaining_daily():.4f}, "
                        f"monthly remaining=${tracker.remaining_monthly():.4f}) -- "
                        "paused fit scoring for the rest of this run"
                    ),
                )
            break

        # Claim it -- mark AI Score Pending *before* the API call, so a
        # failure (parse error, network, exhausted credit balance) leaves
        # this job retryable next run instead of stuck at "Go Score"
        # forever or silently reverting to blank.
        update_my_decision(main_ws, job["id"], MY_DECISION_AI_SCORE_PENDING)

        try:
            result, usage = score_job(
                client, job["title"], job["company_name"] or "Unknown", job["description"] or "", resume_text
            )
        except FitScoreParseError as exc:
            failed += 1
            cost = estimate_cost_usd(SONNET_MODEL, exc.usage["input_tokens"], exc.usage["output_tokens"])
            tracker.record_spend(cost)
            total_cost += cost
            total_input_tokens += exc.usage["input_tokens"]
            total_output_tokens += exc.usage["output_tokens"]
            if workflow_run_id is not None:
                log_step(
                    conn, workflow_run_id=workflow_run_id, job_id=job["id"],
                    step_name="fit_score", step_status="failed", detail=str(exc),
                    tokens_input=exc.usage["input_tokens"], tokens_output=exc.usage["output_tokens"],
                )
            continue  # stays AI Score Pending in the DB and on the Sheet -- retried next run

        cost = estimate_cost_usd(SONNET_MODEL, usage["input_tokens"], usage["output_tokens"])
        tracker.record_spend(cost)
        total_cost += cost
        total_input_tokens += usage["input_tokens"]
        total_output_tokens += usage["output_tokens"]

        conn.execute(
            "INSERT INTO fit_scores (job_id, score, gap_analysis, scored_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (
                job["id"],
                result["score"],
                json.dumps(
                    {
                        "matched_skills": result["matched_skills"],
                        "gaps": result["gaps"],
                        "summary": result["summary"],
                    }
                ),
            ),
        )
        conn.execute("UPDATE jobs SET status = 'scored' WHERE id = ?", (job["id"],))
        # Commit before any Sheets call below -- those can retry/sleep for
        # minutes under quota pressure, and a SQLite write transaction must
        # never stay open across a slow network call: it holds the DB's
        # single write lock the whole time, starving any other process
        # trying to write concurrently (hit live: a second CLI command got
        # "database is locked" errors from exactly this).
        conn.commit()
        scored += 1
        if result["score"] > threshold:
            above_threshold += 1
            update_score(main_ws, job["id"], result["score"])  # Sheets I/O -- also sets My Decision = AI Scored
        else:
            # Below threshold: scoring still completed, so mark AI Scored
            # before evicting -- the Job Log row should reflect that this
            # job WAS scored, not that it's still pending.
            update_my_decision(main_ws, job["id"], MY_DECISION_AI_SCORED)  # Sheets I/O
            if remove_main_row(main_ws, job["id"]):  # Sheets I/O
                conn.execute("UPDATE jobs SET sheet_row_number = NULL WHERE id = ?", (job["id"],))
                conn.commit()  # before the Job Log Sheets call below
            if job_log_ws is not None:
                company = _get_company(conn, job)
                upsert_job_log_row(
                    job_log_ws, job, company, STAGE_SCORED_BELOW_THRESHOLD,
                    fit_score=result["score"], my_decision=MY_DECISION_AI_SCORED,
                )

        if workflow_run_id is not None:
            log_step(
                conn,
                workflow_run_id=workflow_run_id,
                job_id=job["id"],
                step_name="fit_score",
                step_status="ok",
                detail=f"score={result['score']}",
                tokens_input=usage["input_tokens"],
                tokens_output=usage["output_tokens"],
            )

    conn.commit()
    return {
        "evaluated": len(jobs),
        "scored": scored,
        "above_threshold": above_threshold,
        "failed": failed,
        "rejected": rejected,
        "budget_exceeded": budget_exceeded,
        "tokens_input": total_input_tokens,
        "tokens_output": total_output_tokens,
        "estimated_cost_usd": total_cost,
    }
