"""SmartRecruiters targeted-tracking poller — returns all currently open
postings for one company.

Unlike Greenhouse/Lever/Ashby, SmartRecruiters' board is NOT a single-call
API: the list endpoint is paginated (100 postings/page) and does not include
the full description or a human-facing posting URL -- both require a
separate per-posting detail call. Fetching detail for every posting on every
poll would mean 500-1000+ extra HTTP calls per cycle for a large board
(confirmed live: Equinox alone has 666 open postings), so detail is only
fetched for postings not already present in `known_urls` -- an
already-tracked job only needs its URL confirmed (for app.ingest's
closed-job detection), not a fresh description re-fetch.

The posting URL doesn't need the job's name-derived slug -- confirmed live
that "https://jobs.smartrecruiters.com/{company}/{id}" (no slug) resolves
with a 200, so it can be built directly from the list page without a detail
call."""
from __future__ import annotations

from app.http_client import get_json

SMARTRECRUITERS_BASE_URL = "https://api.smartrecruiters.com/v1/companies"
_PAGE_SIZE = 100


def _posting_url(company: str, posting_id: str) -> str:
    return f"https://jobs.smartrecruiters.com/{company}/{posting_id}"


def _fetch_description_html(company: str, posting_id: str, *, session=None) -> str:
    url = f"{SMARTRECRUITERS_BASE_URL}/{company}/postings/{posting_id}"
    data = get_json(url, session=session)
    sections = data.get("jobAd", {}).get("sections", {})
    return "\n\n".join(section.get("text", "") for section in sections.values() if section.get("text"))


def fetch_smartrecruiters_jobs(
    company: str, *, session=None, known_urls: set[str] | None = None
) -> list[dict]:
    stubs: list[dict] = []
    offset = 0
    while True:
        data = get_json(
            f"{SMARTRECRUITERS_BASE_URL}/{company}/postings",
            params={"limit": _PAGE_SIZE, "offset": offset},
            session=session,
        )
        stubs.extend(data.get("content", []))
        offset += _PAGE_SIZE
        if offset >= data.get("totalFound", 0):
            break

    jobs = []
    for item in stubs:
        posting_id = item["id"]
        job_url = _posting_url(company, posting_id)

        description_html = ""
        if known_urls is None or job_url not in known_urls:
            description_html = _fetch_description_html(company, posting_id, session=session)

        location = item.get("location") or {}
        jobs.append(
            {
                "title": (item.get("name") or "").strip(),
                "url": job_url,
                "apply_url": job_url,
                "description_html": description_html,
                "location": location.get("fullLocation"),
                "posted_at": item.get("releasedDate"),
                "company_name": (item.get("company") or {}).get("name") or company,
                "salary_min": None,
                "salary_max": None,
                "salary_source": None,
                "source_type": "smartrecruiters",
            }
        )
    return jobs
