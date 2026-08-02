"""Visa Sponsorship Detection — three tiers, cheapest first:
1. Regex pass for confident restriction/sponsor-friendly phrases (free).
2. Free keyword pre-check: if the description doesn't mention any of
   visa/sponsor/citizen/h1b/work permit/visa permit/work sponsor/visa
   transfer anywhere, classify NO_MENTION for free -- no Haiku call. Added
   after review showed Haiku was handling ~99.7% of postings, most of which
   never reference sponsorship at all.
3. Haiku classification, only for postings that mention one of those
   keywords but aren't cleanly resolved by regex.

Full posting page fetched before all of the above, for every job with a URL
(app.salary_extraction.fetch_job_page_text, the same fetch already built --
but previously never called -- for salary extraction). Root-caused live: the
median stored `description` length across jobs on Beacon is exactly 500
characters, matching Adzuna's `/search` API's server-side truncation, which
covers ~90% of ingested jobs. Sponsorship disclaimers are typically EEO/legal
boilerplate near the END of a real posting, not the start -- e.g. "At this
time, we typically do not offer visa sponsorship for this position" was
confirmed live to be silently invisible to this module for jobs whose
description got cut off before that sentence, misclassifying them
VISA_FLAG_NO_MENTION instead of "restricted" (the keyword pre-check has
nothing to find in truncated text that never contained "sponsor" at all).
The fetch is best-effort: on any failure (no URL, network error, non-200),
classification silently falls back to the stored (possibly truncated)
description rather than failing the job -- one flaky posting's page must
never block the rest of a scan run.

Every job's visa_flag/visa_snippet is stored for audit regardless of whether
require_visa_sponsorship ends up filtering it out. This module only ever
classifies what a specific job posting's own text says -- it never checks
any real record of a company's sponsorship history (DOL LCA filings, USCIS
H-1B data); see app.enrichment's module docstring for that distinct,
currently-unbuilt future item."""
from __future__ import annotations

import json
import re
import sqlite3

from app.budget import BudgetTracker, estimate_cost_usd
from app.job_log import STAGE_VISA_RESTRICTED, upsert_job_log_row
from app.observability import log_step
from app.salary_extraction import fetch_job_page_text
from app.sheets import remove_main_row, update_visa_flag

HAIKU_MODEL = "claude-haiku-4-5"
# Gemini's cheap tier, the Haiku-equivalent for this exact task. Originally
# gemini-2.5-flash-lite (the GA/Standard tier, picked over Preview on the
# reasoning that GA pricing/availability is more stable) -- confirmed live
# against a real, newly-created API key that this reasoning was WRONG:
# gemini-2.5-flash-lite AND gemini-2.5-flash both 404 with "no longer
# available to new users", despite still being listed by
# client.models.list(). gemini-3.5-flash-lite confirmed working live
# against the same key with a normal small max_output_tokens budget.
GEMINI_FLASH_MODEL = "gemini-3.5-flash-lite"

# Internal jobs.visa_flag values for the two new states. Kept as short
# internal tokens, same style as the existing "restricted"/"sponsors"/
# "unclear" -- translating these to the Sheet's user-facing labels
# (Sponsored/No sponsor/No mention/Visa Check Pending/Unclear) is a Sheet
# presentation-layer concern, handled separately during the Beacon rebuild,
# not here.
VISA_FLAG_NO_MENTION = "no_mention"
VISA_FLAG_PENDING = "pending"

# Checked case-insensitively as substrings against the description. If none
# of these appear anywhere, there's nothing for Haiku to usefully read --
# classify NO_MENTION for free instead. This list needs testing against a
# batch of real postings before being trusted as final.
SPONSORSHIP_KEYWORDS = [
    "visa", "sponsor", "citizen", "h1b", "work permit", "visa permit",
    "work sponsor", "visa transfer",
]


def mentions_sponsorship_keywords(text: str) -> bool:
    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in SPONSORSHIP_KEYWORDS)

RESTRICTION_PATTERNS = [
    re.compile(r"no\s+(?:visa\s+)?sponsorship", re.IGNORECASE),
    re.compile(
        r"(?:not|unable to|cannot|can't|won't|will not)\s+(?:currently\s+)?"
        r"(?:provide|offer)?\s*(?:visa\s+)?sponsor(?:ship)?",
        re.IGNORECASE,
    ),
    re.compile(r"does not sponsor", re.IGNORECASE),
    re.compile(r"do not sponsor", re.IGNORECASE),
    # "employer" is common EEO boilerplate alongside/instead of "visa" here --
    # e.g. "without the need for employer sponsorship, now or at any time in
    # the future" -- a real posting's exact wording missed by an earlier,
    # narrower version of this pattern that only allowed "visa" in that slot.
    re.compile(r"without\s+(?:the\s+need\s+for\s+)?(?:employer\s+|visa\s+)?sponsorship", re.IGNORECASE),
    # "...without requiring a visa transfer or visa sponsorship" -- reported
    # live. "Visa transfer" (an H-1B moved between employers) is a distinct
    # restriction from "sponsorship", and this phrasing pairs the two
    # together in the same "without requiring ... or ..." clause -- the
    # existing "without ... sponsorship" pattern above didn't allow that
    # extra clause between "without" and "sponsorship".
    re.compile(r"without\s+(?:the\s+need\s+for\s+)?requiring\s+a\s+visa\s+transfer\s+or\s+(?:employer\s+|visa\s+)?sponsorship", re.IGNORECASE),
    re.compile(r"not\s+(?:currently\s+)?sponsoring", re.IGNORECASE),
    re.compile(r"u\.?s\.?\s+citizens?\s+only", re.IGNORECASE),
    re.compile(r"must be a\s+u\.?s\.?\s+citizen", re.IGNORECASE),
    re.compile(r"green\s*card\s+holders?\s+(?:and|or)\s+(?:u\.?s\.?\s+)?citizens?\s+only", re.IGNORECASE),
    # "Visa sponsorship is not available for this position" -- reported live as
    # very common; missed by the earlier patterns above since they all expect
    # the negation word directly before "sponsor(ship)", not after it.
    re.compile(r"sponsorship\s+is\s+not\s+available", re.IGNORECASE),
    # "...does not now, or in the future, require visa sponsorship..." --
    # standard EEO/legal boilerplate, reported live as very common. Distinct
    # from the "without ... sponsorship, now or at any time" pattern above --
    # different sentence construction ("does not ... require" vs "without").
    re.compile(
        r"does\s+not\s+now,?\s+or\s+(?:in\s+the\s+future|at\s+any\s+time)"
        r",?\s+require\s+(?:visa\s+)?sponsorship",
        re.IGNORECASE,
    ),
    # "This role is not open to VISA Sponsorship" -- reported live. Distinct
    # from the "not sponsor(ship)" pattern above, which only allows
    # "currently"/"provide"/"offer" between "not" and "sponsor" -- "open to"
    # doesn't fit that slot, so this needed its own pattern.
    re.compile(r"not\s+open\s+to\s+(?:visa\s+)?sponsorship", re.IGNORECASE),
]

SPONSOR_PATTERNS = [
    re.compile(r"(?:we|company)?\s*will\s+sponsor", re.IGNORECASE),
    re.compile(r"visa\s+sponsorship\s+(?:is\s+)?available", re.IGNORECASE),
    re.compile(r"sponsorship\s+available", re.IGNORECASE),
    re.compile(r"h-?1b\s+sponsorship", re.IGNORECASE),
    re.compile(r"open\s+to\s+sponsor", re.IGNORECASE),
    re.compile(r"sponsorship\s+provided", re.IGNORECASE),
    re.compile(r"will\s+sponsor\s+(?:work\s+)?visas?", re.IGNORECASE),
]

_SNIPPET_CONTEXT = 80

HAIKU_PROMPT = """You are screening a job description for visa sponsorship language, for a
candidate applying to this specific posting at its listed location.

Job title: {title}
Job location: {location}

Classify whether the employer sponsors a US work visa (e.g. H-1B) for THIS
SPECIFIC posting. Pay close attention to sponsorship language scoped to a
different country or office than this posting's location -- e.g. "we can
sponsor visas to Germany" on a US-based posting means NO US sponsorship, so
that's "restricted", not "sponsors", even though the word "sponsor" appears
in a positive sentence.

Respond with:
- "restricted" if the posting states or implies the employer will NOT sponsor a visa for THIS location (e.g. requires existing work authorization without sponsorship, US citizens/green card holders only, or sponsorship is offered only for a different country/office than this one)
- "sponsors" if the posting states the employer will sponsor a visa for THIS location
- "unclear" if the posting says nothing about visa sponsorship for this location either way

Job description:
{description}
"""

VISA_SCHEMA = {
    "type": "object",
    "properties": {
        "visa_flag": {"type": "string", "enum": ["restricted", "sponsors", "unclear"]},
        "snippet": {
            "type": "string",
            "description": "Short quote from the description supporting the classification, or empty string if unclear.",
        },
    },
    "required": ["visa_flag", "snippet"],
    "additionalProperties": False,
}


def _snippet(text: str, match: re.Match) -> str:
    start = max(0, match.start() - _SNIPPET_CONTEXT)
    end = min(len(text), match.end() + _SNIPPET_CONTEXT)
    return text[start:end].strip()


def regex_classify(description: str) -> tuple[str | None, str | None]:
    """Returns (visa_flag, snippet), or (None, None) if ambiguous (no confident match)."""
    for pattern in RESTRICTION_PATTERNS:
        match = pattern.search(description)
        if match:
            return "restricted", _snippet(description, match)
    for pattern in SPONSOR_PATTERNS:
        match = pattern.search(description)
        if match:
            return "sponsors", _snippet(description, match)
    return None, None


def haiku_classify(
    client, description: str, title: str = "", location: str = ""
) -> tuple[dict, dict]:
    # Visa/sponsorship language is frequently in EEO/legal boilerplate near the
    # END of a JD, not the start -- a short prefix truncation can (and did, in
    # testing) cut off exactly the sentence that matters. 12000 chars is ~3000
    # tokens; Haiku is cheap enough that this isn't a meaningful cost concern.
    #
    # title/location are passed so the model can correctly handle postings
    # whose sponsorship language is scoped to a different country/office than
    # this specific listing (e.g. "we can sponsor visas to Germany" on a
    # US-based posting means NO US sponsorship) -- confirmed against a real
    # posting during testing where omitting this context caused a misclassification.
    response = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=256,
        temperature=0,
        output_config={"format": {"type": "json_schema", "schema": VISA_SCHEMA}},
        messages=[{
            "role": "user",
            "content": HAIKU_PROMPT.format(
                title=title or "(unknown)",
                location=location or "(unknown)",
                description=description[:12000],
            ),
        }],
    )
    text = next(block.text for block in response.content if block.type == "text")
    result = json.loads(text)
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    return result, usage


def gemini_classify(
    client, description: str, title: str = "", location: str = ""
) -> tuple[dict, dict]:
    """Gemini equivalent of haiku_classify -- identical prompt and forced
    JSON schema, identical (result, usage) return shape, so run_visa_scan's
    provider dispatch can call either one without caring which it is."""
    response = client.models.generate_content(
        model=GEMINI_FLASH_MODEL,
        contents=HAIKU_PROMPT.format(
            title=title or "(unknown)",
            location=location or "(unknown)",
            description=description[:12000],
        ),
        config={
            "response_mime_type": "application/json",
            "response_json_schema": VISA_SCHEMA,
            "temperature": 0,
            "max_output_tokens": 256,
        },
    )
    result = json.loads(response.text)
    usage = {
        "input_tokens": response.usage_metadata.prompt_token_count,
        "output_tokens": response.usage_metadata.candidates_token_count,
    }
    return result, usage


# Maps LLM_PROVIDER -> (classify_fn, model_name_for_cost_estimation). Both
# classify_fns share the exact (client, description, title=, location=) ->
# (result, usage) contract, so run_visa_scan's dispatch is a single lookup,
# not a branch per provider.
VISA_CLASSIFIERS = {
    "anthropic": (haiku_classify, HAIKU_MODEL),
    "gemini": (gemini_classify, GEMINI_FLASH_MODEL),
}


def _evict_restricted_job(
    conn: sqlite3.Connection, main_ws, job_log_ws, job: sqlite3.Row, snippet: str | None,
) -> bool:
    """Shared eviction logic for a job whose visa_flag is "restricted" and
    require_visa_sponsorship is on -- used both by the per-job classification
    loop below and by evict_already_restricted_jobs (for jobs classified
    "restricted" under an earlier require_visa_sponsorship=False, which never
    went through this path the first time). Returns True if a Beacon row was
    actually found and removed."""
    removed = False
    if main_ws is not None:
        removed = remove_main_row(main_ws, job["id"])  # Sheets I/O
        if removed:
            conn.execute("UPDATE jobs SET sheet_row_number = NULL WHERE id = ?", (job["id"],))
            conn.commit()  # before any further Sheets I/O

    conn.execute(
        "UPDATE jobs SET status = 'filtered_out', rejection_reason = ? WHERE id = ?",
        (STAGE_VISA_RESTRICTED, job["id"]),
    )
    conn.commit()  # before the Job Log Sheets call below

    if job_log_ws is not None:
        company = None
        if job["company_id"] is not None:
            company = conn.execute("SELECT * FROM companies WHERE id = ?", (job["company_id"],)).fetchone()
        reason = f"{STAGE_VISA_RESTRICTED}: {snippet}" if snippet else STAGE_VISA_RESTRICTED
        upsert_job_log_row(job_log_ws, job, company, reason)

    return removed


def evict_already_restricted_jobs(conn: sqlite3.Connection, main_ws, job_log_ws) -> int:
    """Sweeps jobs already classified visa_flag="restricted" that are still
    on Beacon (sheet_row_number IS NOT NULL). Needed because run_visa_scan's
    own query below only re-evaluates NULL/pending jobs -- a job classified
    "restricted" while require_visa_sponsorship was False stays on Beacon
    forever afterward, even once the setting is flipped True, since it's
    never re-selected for classification again. Call this once at the start
    of every run_visa_scan call (cheap no-op once the backlog is cleared --
    a single indexed-by-nothing but small-result-set query). Returns the
    number of jobs evicted."""
    stuck = conn.execute(
        "SELECT * FROM jobs WHERE visa_flag = 'restricted' AND sheet_row_number IS NOT NULL"
    ).fetchall()
    evicted = 0
    for job in stuck:
        if _evict_restricted_job(conn, main_ws, job_log_ws, job, job["visa_snippet"]):
            evicted += 1
    return evicted


def run_visa_scan(
    conn: sqlite3.Connection,
    client,
    settings: dict,
    limit: int | None = None,
    workflow_run_id: int | None = None,
    job_log_ws=None,
    main_ws=None,
    provider: str = "anthropic",
) -> dict:
    # `client` must already match `provider` (an anthropic.Anthropic client
    # for "anthropic", a genai.Client for "gemini") -- see app.llm_provider,
    # the one place that builds the right one from Settings.
    classify, model_name = VISA_CLASSIFIERS.get(provider, (haiku_classify, HAIKU_MODEL))

    # Catches jobs classified "restricted" under an earlier
    # require_visa_sponsorship=False that the query below will never
    # re-select (see evict_already_restricted_jobs's docstring) -- without
    # this, flipping the setting True doesn't retroactively evict anything
    # already classified before the flip.
    already_evicted = 0
    if settings.get("require_visa_sponsorship"):
        already_evicted = evict_already_restricted_jobs(conn, main_ws, job_log_ws)

    # Picks up never-scanned jobs AND jobs stuck at VISA_FLAG_PENDING from a
    # prior run whose Haiku call failed -- both get retried the same way.
    query = (
        "SELECT * FROM jobs WHERE status = 'new' "
        "AND (visa_flag IS NULL OR visa_flag = ?)"
    )
    params: tuple = (VISA_FLAG_PENDING,)
    if limit is not None:
        query += " LIMIT ?"
        params = params + (limit,)
    jobs = conn.execute(query, params).fetchall()

    # Was documented in app.budget's own module docstring as something that
    # "could be reused for Haiku visa classification too" but never actually
    # wired in -- this loop ran fully unconstrained by daily_token_budget/
    # monthly_token_budget, unlike run_fit_scoring's identical use of this
    # same tracker. Live-reported directly: cost is not "trivial" at this
    # user's threshold, so an unenforced budget setting is a real gap, not
    # a cosmetic one.
    tracker = BudgetTracker(conn, settings.get("daily_token_budget"), settings.get("monthly_token_budget"))

    regex_hits = no_mention_count = haiku_calls = haiku_failures = restricted_filtered = 0
    budget_exceeded = False
    total_input_tokens = total_output_tokens = 0
    total_cost = 0.0

    for job in jobs:
        description = job["description"] or ""
        try:
            full_text = fetch_job_page_text(job["url"])
        except Exception as exc:  # noqa: BLE001 -- best-effort; stored description is the fallback
            full_text = None
            if workflow_run_id is not None:
                log_step(
                    conn, workflow_run_id=workflow_run_id, job_id=job["id"],
                    step_name="visa_scan", step_status="page_fetch_failed", detail=str(exc),
                )
        if full_text:
            description = full_text

        visa_flag, snippet = regex_classify(description)
        tokens_input = tokens_output = 0

        if visa_flag is None:
            if not mentions_sponsorship_keywords(description):
                visa_flag, snippet = VISA_FLAG_NO_MENTION, None
                no_mention_count += 1
            else:
                if not tracker.has_budget():
                    budget_exceeded = True
                    if workflow_run_id is not None:
                        log_step(
                            conn, workflow_run_id=workflow_run_id, job_id=job["id"],
                            step_name="visa_scan", step_status="CRITICAL",
                            detail=(
                                f"Token budget exceeded (daily remaining=${tracker.remaining_daily():.4f}, "
                                f"monthly remaining=${tracker.remaining_monthly():.4f}) -- "
                                "paused visa scanning for the rest of this run"
                            ),
                        )
                    break  # remaining jobs stay unclassified/pending -- retried next run

                # Needs Haiku. Mark pending *before* attempting the call --
                # if it fails (rate limit, exhausted credit balance, network
                # error), this job stays retryable next run instead of
                # crashing the rest of this batch or silently staying blank.
                conn.execute(
                    "UPDATE jobs SET visa_flag = ? WHERE id = ?", (VISA_FLAG_PENDING, job["id"]),
                )
                conn.commit()
                if main_ws is not None:
                    update_visa_flag(main_ws, job["id"], VISA_FLAG_PENDING)

                try:
                    result, usage = classify(
                        client, description, title=job["title"] or "", location=job["location"] or ""
                    )
                except Exception as exc:  # noqa: BLE001 -- one bad call must not kill the rest of the batch
                    haiku_failures += 1
                    if workflow_run_id is not None:
                        log_step(
                            conn, workflow_run_id=workflow_run_id, job_id=job["id"],
                            step_name="visa_scan", step_status="haiku_failed", detail=str(exc),
                        )
                    continue  # stays VISA_FLAG_PENDING in the DB and on the Sheet -- retried next run

                haiku_calls += 1
                visa_flag = result["visa_flag"]
                snippet = result.get("snippet") or None
                tokens_input, tokens_output = usage["input_tokens"], usage["output_tokens"]
                total_input_tokens += tokens_input
                total_output_tokens += tokens_output
                cost = estimate_cost_usd(model_name, tokens_input, tokens_output)
                total_cost += cost
                tracker.record_spend(cost)
        else:
            regex_hits += 1

        conn.execute(
            "UPDATE jobs SET visa_flag = ?, visa_snippet = ? WHERE id = ?",
            (visa_flag, snippet, job["id"]),
        )
        # Commit now, before any Sheets call below -- those can retry/sleep
        # for minutes under quota pressure, and a SQLite write transaction
        # must never stay open across a slow network call: it holds the DB's
        # single write lock the whole time, starving any other process
        # trying to write concurrently (hit live: a second CLI command got
        # "database is locked" errors from exactly this).
        conn.commit()

        restricted_and_required = settings.get("require_visa_sponsorship") and visa_flag == "restricted"

        if restricted_and_required:
            _evict_restricted_job(conn, main_ws, job_log_ws, job, snippet)
            restricted_filtered += 1
        elif main_ws is not None:
            update_visa_flag(main_ws, job["id"], visa_flag)  # Sheets I/O

        if workflow_run_id is not None:
            log_step(
                conn,
                workflow_run_id=workflow_run_id,
                job_id=job["id"],
                step_name="visa_scan",
                step_status=visa_flag,
                detail=snippet,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
            )

    conn.commit()
    return {
        "scanned": len(jobs),
        "already_restricted_evicted": already_evicted,
        "regex_hits": regex_hits,
        "no_mention": no_mention_count,
        "haiku_calls": haiku_calls,
        "haiku_failures": haiku_failures,
        "restricted_filtered": restricted_filtered,
        "budget_exceeded": budget_exceeded,
        "tokens_input": total_input_tokens,
        "tokens_output": total_output_tokens,
        "estimated_cost_usd": total_cost,
    }
