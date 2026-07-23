"""Posted salary range extraction from job description text — deliberately
NOT from Adzuna's salary_min/salary_max fields, which are frequently an
algorithmic estimate rather than what the employer actually posted (confirmed
on a real listing: Adzuna showed $293,969 predicted while the actual posting
stated "$140,000 - $200,000"). jobs.salary_min/salary_max is left unset at
ingestion for Adzuna-sourced jobs (see app.sources.adzuna) -- it's populated
exclusively by extraction from real JD text, whether that's the cheap
description-text pass here or the page-fetch fallback below. Adzuna's own
estimate is kept separately and permanently in jobs.adzuna_salary_min/max.

Some sources (Greenhouse/Lever/Ashby) already store the full description, so
description-text extraction alone often succeeds. Adzuna's own API caps
descriptions at ~500 chars, so its stored text frequently doesn't include the
salary line at all -- for those, fall back to fetching the full posting page.
"""
from __future__ import annotations

import re

from app.html_strip import strip_html
from app.http_client import BROWSER_HEADERS, request_with_retry

_SALARY_RANGE_RE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})+|\d{1,3}[kK])\s*(?:-|–|—|to)\s*\$?\s?(\d{1,3}(?:,\d{3})+|\d{1,3}[kK])"
)

_MIN_PLAUSIBLE_SALARY = 10_000
_MAX_PLAUSIBLE_SALARY = 2_000_000


def _parse_amount(raw: str) -> int:
    raw = raw.strip()
    if raw[-1] in "kK":
        return int(float(raw[:-1].replace(",", "")) * 1000)
    return int(raw.replace(",", ""))


def extract_salary_range(text: str | None) -> tuple[int | None, int | None]:
    if not text:
        return None, None

    match = _SALARY_RANGE_RE.search(text)
    if not match:
        return None, None

    low = _parse_amount(match.group(1))
    high = _parse_amount(match.group(2))
    if low > high:
        low, high = high, low

    if low < _MIN_PLAUSIBLE_SALARY or high > _MAX_PLAUSIBLE_SALARY:
        return None, None

    return low, high


def fetch_job_page_text(url: str, session=None) -> str:
    kwargs = {"headers": BROWSER_HEADERS, "timeout": 15}
    if session is not None:
        kwargs["session"] = session
    response = request_with_retry("GET", url, **kwargs)
    response.raise_for_status()
    return strip_html(response.text)


def resolve_posted_salary(
    description: str | None, url: str | None, session=None
) -> tuple[int | None, int | None]:
    """Tries the stored description first (free, no network); only fetches
    the full posting page if that doesn't yield a match."""
    low, high = extract_salary_range(description)
    if low is not None:
        return low, high

    if not url:
        return None, None

    try:
        page_text = fetch_job_page_text(url, session=session)
    except Exception:
        return None, None

    return extract_salary_range(page_text)
