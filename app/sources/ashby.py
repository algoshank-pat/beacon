"""Ashby targeted-tracking poller — returns all currently open postings for one org."""
from __future__ import annotations

from app.http_client import get_json

ASHBY_BASE_URL = "https://api.ashbyhq.com/posting-api/job-board"


def fetch_ashby_jobs(org_slug: str, *, session=None) -> list[dict]:
    url = f"{ASHBY_BASE_URL}/{org_slug}"
    data = get_json(url, session=session)

    jobs = []
    for item in data.get("jobs", []):
        jobs.append(
            {
                "title": (item.get("title") or "").strip(),
                "url": item.get("jobUrl") or item.get("applyUrl"),
                "apply_url": item.get("applyUrl") or item.get("jobUrl"),
                "description_html": item.get("descriptionHtml") or item.get("descriptionPlain", ""),
                "location": item.get("location"),
                "posted_at": item.get("publishedAt"),
                "company_name": org_slug,
                "salary_min": None,
                "salary_max": None,
                "salary_source": None,
                "source_type": "ashby",
            }
        )
    return jobs
