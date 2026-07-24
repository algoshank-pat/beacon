"""Filter Engine — evaluates jobs against live filter_settings/filter_keywords.

Evaluation order (cheapest first, matching the spec): keyword match (role/tech
include + title_exclude) -> seniority -> remote/location -> posted date ->
company attributes. Only passing jobs proceed to visa scan and fit scoring.

Note: nothing in the ingestion pollers populates jobs.seniority/remote_type
directly (none of Adzuna/Greenhouse/Lever/Ashby reliably expose a clean
seniority or remote flag), so this module infers both from title/location
text the first time a job is evaluated and persists the inferred value.
Unknown/unparseable signals are treated as passing (permissive) rather than
filtered out, since a missing signal isn't evidence of a mismatch.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from app.dates import parse_datetime
from app.filter_settings import get_active_keywords, get_filter_settings
from app.job_log import upsert_job_log_row
from app.location import is_us_location
from app.observability import log_step
from app.sheets import add_job_to_beacon

PRIORITY_ORDER = {"S": 4, "A": 3, "B": 2, "C": 1}

KEYWORD_CATEGORIES = [
    "role_keyword_include",
    "tech_keyword_include",
    "title_exclude",
    "seniority",
    "remote_type",
    "location_include",
    "industries_include",
]

_SENIORITY_MARKERS = [
    (re.compile(r"\b(staff|principal)\b", re.IGNORECASE), "staff"),
    (re.compile(r"\b(sr\.?|senior)\b", re.IGNORECASE), "senior"),
    (re.compile(r"\b(jr\.?|junior|entry[\s-]?level|intern)\b", re.IGNORECASE), "junior"),
]


@dataclass
class FilterResult:
    passed: bool
    stage: str | None = None
    reason: str | None = None
    detail: str | None = None
    skip_log: bool = False


def infer_seniority(title: str) -> str:
    for pattern, level in _SENIORITY_MARKERS:
        if pattern.search(title or ""):
            return level
    return "mid"


def infer_remote_type(location: str | None) -> str | None:
    if not location:
        return None
    lowered = location.lower()
    if "remote" in lowered:
        return "remote"
    if "hybrid" in lowered:
        return "hybrid"
    return None


def _contains_any(haystack: str, needles: list[str]) -> bool:
    lowered = (haystack or "").lower()
    return any(needle.lower() in lowered for needle in needles)



# Keyword-specific exclusions for confirmed real-world false-positive
# collisions -- each added only after finding the actual bad match live, not
# guessed at preemptively:
# - "X12" (EDI standard, tech_keyword_include) collides with healthcare
#   shift-count notation ("1 to 2 x12-Hour Shifts/Week") -- a genuine
#   leading word boundary exists there too, so excluded via a lookahead for
#   an immediately-following "-hour"/" hour" instead.
# - "Kong" (tracked API-gateway company, tech_keyword_include) collides
#   with the place name "Hong Kong" -- confirmed live: 13 unrelated jobs
#   (a restaurant "Executive Chef" posting, an MBA leadership program, a
#   bilingual wholesale rep, etc., across companies with no relation to
#   Kong the company) passed the keyword gate purely because their
#   description mentioned "Hong Kong" somewhere (an office location, a
#   cuisine style...). Excluded via a lookbehind for an immediately-
#   preceding "hong "/"hong-".
_LOOKAHEAD_EXCLUSIONS = {"x12": r"(?!-?\s*hour)"}
_LOOKBEHIND_EXCLUSIONS = {"kong": r"(?<!hong[\s-])"}


def _contains_any_word(haystack: str, needles: list[str]) -> bool:
    """Requires a word boundary immediately BEFORE the needle (not after) --
    used for role/tech include keywords. Blocks a short keyword like "X12"
    (an EDI standard, in tech_keyword_include) from matching when it's
    embedded inside an unrelated word with no separator -- a real false
    positive seen live: a nursing posting's "13 Week" duration and "x12"
    shift-count notation got concatenated with no space by Adzuna's scraped
    text ("Weekx12"), unrelated to EDI entirely.

    Deliberately NOT anchored at the end too: role/tech keywords are meant to
    also match their common suffix/plural forms as a prefix -- "Architect"
    should still match inside "Architecture", "Engineer" inside
    "Engineering", "Integration" inside "Integrations". Only the leading
    edge needs to be a real word start, not a mid-word splice.

    Also applies any keyword-specific exclusion from _LOOKAHEAD_EXCLUSIONS/
    _LOOKBEHIND_EXCLUSIONS (see above) -- these are per-keyword, not global,
    since the collisions themselves are specific to one keyword's exact
    text, not a property every keyword shares."""
    lowered = (haystack or "").lower()
    for needle in needles:
        needle_lower = needle.lower()
        lookbehind = _LOOKBEHIND_EXCLUSIONS.get(needle_lower, "")
        lookahead = _LOOKAHEAD_EXCLUSIONS.get(needle_lower, "")
        pattern = rf"{lookbehind}\b{re.escape(needle_lower)}{lookahead}"
        if re.search(pattern, lowered) is not None:
            return True
    return False


def evaluate_job(
    job: sqlite3.Row,
    company: sqlite3.Row | None,
    settings: dict,
    keywords: dict[str, list[str]],
) -> FilterResult:
    title = job["title"] or ""
    description = job["description"] or ""
    combined = f"{title}\n{description}"

    role_kw = keywords.get("role_keyword_include", [])
    tech_kw = keywords.get("tech_keyword_include", [])
    title_exclude = keywords.get("title_exclude", [])

    # A handful of tech_keyword_include entries are also the name of a
    # tracked company (Kong, Boomi -- both API-Gateway/iPaaS vendors whose
    # own product name IS their company name). Every posting from that
    # company mentions its own name somewhere in ordinary boilerplate
    # ("...loyalty to Kong", "Boomi Embedded"), which trivially "matched" the
    # keyword regardless of the actual role -- real false positives seen
    # live (Renewal Account Representative, Sales Development Representative
    # at Kong; Product Marketing Manager at Boomi). The keyword still counts
    # normally for any OTHER company's postings that mention needing that
    # tool/vendor as a skill.
    if company is not None and company["name"]:
        company_name = company["name"].strip().lower()
        tech_kw = [kw for kw in tech_kw if kw.strip().lower() != company_name]
        role_kw = [kw for kw in role_kw if kw.strip().lower() != company_name]

    matched = _contains_any_word(combined, role_kw) or _contains_any_word(combined, tech_kw)
    if not matched:
        return FilterResult(
            False, "keyword", "Filtered Out - Title/Keyword Mismatch",
            "no role/tech keyword matched title or description",
        )
    if title_exclude and _contains_any(title, title_exclude):
        return FilterResult(
            False, "keyword", "Filtered Out - Title/Keyword Mismatch",
            "title matched a title_exclude term",
        )

    seniority_filter = [s.lower() for s in keywords.get("seniority", [])]
    job_seniority = job["seniority"] or infer_seniority(title)
    if seniority_filter and job_seniority.lower() not in seniority_filter:
        return FilterResult(
            False, "seniority", "Filtered Out - Seniority",
            f"seniority '{job_seniority}' not in {seniority_filter}",
        )

    remote_filter = [r.lower() for r in keywords.get("remote_type", [])]
    job_remote = job["remote_type"] or infer_remote_type(job["location"])
    if remote_filter and job_remote and job_remote.lower() not in remote_filter:
        return FilterResult(
            False, "location", "Filtered Out - Location",
            f"remote_type '{job_remote}' not in {remote_filter}",
        )

    # Matches the resolved, clean location_state field (a 2-letter state
    # code or "Remote-USA" -- see app.location_state), not the raw location
    # text -- location_state exists specifically to resolve messy source
    # strings, so filtering should use it instead of re-doing a fragile
    # substring match against the same inconsistent text it was built to
    # clean up. Exact match, not substring -- state codes are precise
    # discrete values, unlike free text. Permissive when location_state is
    # blank (unresolved, e.g. an informal place name or non-US remote
    # posting) -- same "missing signal isn't evidence of a mismatch" rule
    # this module already applies to remote_type just above.
    location_filter = [s.lower() for s in keywords.get("location_include", [])]
    job_location_state = job["location_state"]
    if location_filter and job_location_state and job_location_state.lower() not in location_filter:
        return FilterResult(
            False, "location", "Filtered Out - Location",
            f"location_state '{job_location_state}' not in {location_filter}",
        )

    if settings.get("require_us_location") and not is_us_location(job["location"]):
        # Non-US jobs are high-volume noise, not worth a Job Log row -- per
        # direct request, these are silently filtered out with no logging.
        return FilterResult(
            False, "location", "Filtered Out - Location",
            f"location '{job['location']}' does not appear to be within the United States",
            skip_log=True,
        )

    posted_within_days = settings.get("posted_within_days")
    if posted_within_days is not None and job["posted_at"]:
        posted_at = parse_datetime(job["posted_at"])
        if posted_at is not None:
            age_days = (datetime.now(timezone.utc) - posted_at).days
            if age_days > posted_within_days:
                return FilterResult(
                    False, "posted_date", "Filtered Out - Posted Date",
                    f"posted {age_days}d ago, exceeds {posted_within_days}d window",
                )

    if company is not None:
        priority_min = settings.get("company_priority_min")
        if priority_min and company["priority_tier"]:
            if PRIORITY_ORDER.get(company["priority_tier"], 0) < PRIORITY_ORDER.get(priority_min, 0):
                return FilterResult(
                    False, "company", "Filtered Out - Company Criteria",
                    f"priority_tier '{company['priority_tier']}' below minimum '{priority_min}'",
                )

        emp_min = settings.get("employee_count_min")
        emp_max = settings.get("employee_count_max")
        if company["employee_count"] is not None:
            if emp_min is not None and company["employee_count"] < emp_min:
                return FilterResult(
                    False, "company", "Filtered Out - Company Criteria",
                    f"employee_count {company['employee_count']} < min {emp_min}",
                )
            if emp_max is not None and company["employee_count"] > emp_max:
                return FilterResult(
                    False, "company", "Filtered Out - Company Criteria",
                    f"employee_count {company['employee_count']} > max {emp_max}",
                )

        founded_after = settings.get("founded_after_year")
        if founded_after is not None and company["founded_year"] is not None and company["founded_year"] < founded_after:
            return FilterResult(
                False, "company", "Filtered Out - Company Criteria",
                f"founded_year {company['founded_year']} before {founded_after}",
            )

        industries_filter = keywords.get("industries_include", [])
        if industries_filter and company["industry"] and not _contains_any(company["industry"], industries_filter):
            return FilterResult(
                False, "company", "Filtered Out - Company Criteria",
                f"industry '{company['industry']}' does not match {industries_filter}",
            )

        if settings.get("require_h1b_track_record") and not company["h1b_sponsor_last_5yrs"]:
            return FilterResult(
                False, "company", "Filtered Out - Company Criteria",
                "require_h1b_track_record is set but company has no confirmed H-1B track record",
            )

    return FilterResult(True)


def run_filter_engine(
    conn: sqlite3.Connection, workflow_run_id: int | None = None, job_log_ws=None, main_ws=None
) -> dict:
    settings = get_filter_settings(conn)
    keywords = {cat: get_active_keywords(conn, cat) for cat in KEYWORD_CATEGORIES}

    jobs = conn.execute("SELECT * FROM jobs WHERE status = 'new'").fetchall()

    passed = filtered_out = 0
    for job in jobs:
        company = None
        if job["company_id"] is not None:
            company = conn.execute(
                "SELECT * FROM companies WHERE id = ?", (job["company_id"],)
            ).fetchone()

        result = evaluate_job(job, company, settings, keywords)

        if result.passed:
            inferred_seniority = job["seniority"] or infer_seniority(job["title"] or "")
            inferred_remote = job["remote_type"] or infer_remote_type(job["location"])
            conn.execute(
                "UPDATE jobs SET seniority = ?, remote_type = ? WHERE id = ?",
                (inferred_seniority, inferred_remote, job["id"]),
            )
            # Commit before any Sheets call below -- those can retry/sleep for
            # minutes under quota pressure (see app.sheets_retry), and a SQLite
            # write transaction must never stay open across a slow network
            # call: it holds the DB's single write lock the whole time,
            # starving any other process trying to write concurrently (hit
            # live: a second CLI command got "database is locked" errors).
            conn.commit()
            passed += 1
            if main_ws is not None:
                add_job_to_beacon(conn, main_ws, job, company, workflow_run_id=workflow_run_id)
        else:
            conn.execute(
                "UPDATE jobs SET status = 'filtered_out', rejection_reason = ? WHERE id = ?",
                (result.reason, job["id"]),
            )
            conn.commit()
            filtered_out += 1
            if job_log_ws is not None and not result.skip_log:
                reason = f"{result.reason}: {result.detail}" if result.detail else result.reason
                upsert_job_log_row(job_log_ws, job, company, reason)

        if workflow_run_id is not None:
            log_step(
                conn,
                workflow_run_id=workflow_run_id,
                job_id=job["id"],
                step_name="filter",
                step_status="passed" if result.passed else "filtered_out",
                detail=result.detail,
            )

    conn.commit()
    return {"evaluated": len(jobs), "passed": passed, "filtered_out": filtered_out}
