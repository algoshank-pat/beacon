"""Adzuna broad-discovery poller — one query per active keyword."""
from __future__ import annotations

from app.http_client import get_json

ADZUNA_BASE_URL = "https://api.adzuna.com/v1/api/jobs"


def _to_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_adzuna_jobs_for_keyword(
    app_id: str,
    app_key: str,
    keyword: str,
    *,
    country: str = "us",
    location: str | None = None,
    max_days_old: int = 30,
    page: int = 1,
    results_per_page: int = 50,
    session=None,
) -> list[dict]:
    """One query per keyword, using what_phrase for exact multi-word matching.

    Adzuna's phrase search checks title *and* description, matching the Filter
    Engine's own title+description scan for ATS-sourced jobs.
    """
    url = f"{ADZUNA_BASE_URL}/{country}/search/{page}"
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "what_phrase": keyword,
        "max_days_old": max_days_old,
        "results_per_page": results_per_page,
        "content-type": "application/json",
    }
    if location:
        params["where"] = location

    data = get_json(url, params=params, session=session)

    jobs = []
    for result in data.get("results", []):
        jobs.append(
            {
                "title": (result.get("title") or "").strip(),
                "url": result.get("redirect_url"),
                "apply_url": result.get("redirect_url"),
                "description_html": result.get("description", ""),
                "location": (result.get("location") or {}).get("display_name"),
                "posted_at": result.get("created"),
                "company_name": (result.get("company") or {}).get("display_name", "Unknown"),
                # salary_min/salary_max are deliberately left unset here, not
                # filled with Adzuna's estimate -- those columns are reserved
                # exclusively for a range parsed from real JD text (see
                # app.salary_extraction / app.sheets.add_job_to_beacon).
                # Adzuna's own algorithmic estimate is frequently wrong vs.
                # what the employer actually posted (confirmed live: Adzuna
                # showed $293,969 for a listing whose real text stated
                # "$140,000 - $200,000"). It's kept instead, always, in the
                # separate adzuna_salary_min/max columns below.
                "salary_min": None,
                "salary_max": None,
                "salary_source": None,
                "adzuna_salary_min": _to_int(result.get("salary_min")),
                "adzuna_salary_max": _to_int(result.get("salary_max")),
                "source_type": "adzuna",
            }
        )
    return jobs
