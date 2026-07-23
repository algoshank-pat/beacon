"""Greenhouse targeted-tracking poller — returns all currently open postings for one board."""
from __future__ import annotations

from app.http_client import get_json

GREENHOUSE_BASE_URL = "https://boards-api.greenhouse.io/v1/boards"


def fetch_greenhouse_jobs(board_token: str, *, session=None) -> list[dict]:
    url = f"{GREENHOUSE_BASE_URL}/{board_token}/jobs"
    data = get_json(url, params={"content": "true"}, session=session)

    jobs = []
    for item in data.get("jobs", []):
        jobs.append(
            {
                "title": (item.get("title") or "").strip(),
                "url": item.get("absolute_url"),
                "apply_url": item.get("absolute_url"),
                "description_html": item.get("content", ""),
                "location": (item.get("location") or {}).get("name"),
                "posted_at": item.get("first_published") or item.get("updated_at"),
                "company_name": item.get("company_name") or board_token,
                "salary_min": None,
                "salary_max": None,
                "salary_source": None,
                "source_type": "greenhouse",
            }
        )
    return jobs
