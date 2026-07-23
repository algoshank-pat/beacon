"""Lever targeted-tracking poller — returns all currently open postings for one company."""
from __future__ import annotations

from datetime import datetime, timezone

from app.http_client import get_json

LEVER_BASE_URL = "https://api.lever.co/v0/postings"


def _posted_at(created_ms) -> str | None:
    if not created_ms:
        return None
    return datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).isoformat()


def fetch_lever_jobs(company: str, *, session=None) -> list[dict]:
    url = f"{LEVER_BASE_URL}/{company}"
    data = get_json(url, params={"mode": "json"}, session=session)

    jobs = []
    for item in data:
        categories = item.get("categories") or {}
        jobs.append(
            {
                "title": (item.get("text") or "").strip(),
                "url": item.get("hostedUrl"),
                "apply_url": item.get("applyUrl") or item.get("hostedUrl"),
                "description_html": item.get("description") or item.get("descriptionPlain", ""),
                "location": categories.get("location"),
                "posted_at": _posted_at(item.get("createdAt")),
                "company_name": company,
                "salary_min": None,
                "salary_max": None,
                "salary_source": None,
                "source_type": "lever",
            }
        )
    return jobs
