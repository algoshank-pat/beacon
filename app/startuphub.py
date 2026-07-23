"""StartupHub.ai — free-tier company metadata (HQ, founded year, industry
sectors) for companies in their AI-startup directory. Does NOT cover
employee_count/funding_stage/revenue_or_valuation on the free tier (those
require a "Pro Lite or higher" plan for the /enrich endpoint, which the
current key doesn't have access to -- confirmed via a live 403).

The `q=` query parameter is the only one that actually searches by name.
`query=`/`name=`/`search=` are silently ignored and return an unrelated
default listing (confirmed live: `name=Workato` returned Medtronic as the
top result) -- a real footgun, not a hypothetical one. Because of that, the
top search result's name is also verified against the query before use,
rather than trusted blindly (verified live: a `q=Zapier` search also
surfaced an unrelated second result, "Molvrix").
"""
from __future__ import annotations

import re

from app.http_client import get_json

BASE_URL = "https://www.startuphub.ai/api/v1/startups"


def search_startup(company_name: str, api_key: str, session=None) -> dict | None:
    data = get_json(
        BASE_URL,
        params={"q": company_name},
        headers={"Authorization": f"Bearer {api_key}"},
        session=session,
    )
    results = data.get("data") or []
    for result in results:
        if result.get("name", "").strip().lower() == company_name.strip().lower():
            return result
    return None


def _founded_year(founded_date: str | None) -> int | None:
    if not founded_date:
        return None
    match = re.match(r"(\d{4})", founded_date)
    return int(match.group(1)) if match else None


def _hq_location(profile: dict) -> str | None:
    city, country = profile.get("hq_city"), profile.get("hq_country")
    if city and country:
        return f"{city}, {country}"
    return city or country or None


def fetch_company_profile(company_name: str, api_key: str, session=None) -> dict | None:
    """Returns a partial companies-table-shaped dict (hq_location, founded_year,
    industry only -- see module docstring for why) or None if not found in
    StartupHub's directory."""
    profile = search_startup(company_name, api_key, session=session)
    if profile is None:
        return None

    sectors = profile.get("sectors") or []

    return {
        "employee_count": None,
        "employee_count_range": None,
        "hq_location": _hq_location(profile),
        "company_type": None,
        "funding_stage": None,
        "revenue_or_valuation": None,
        "revenue_valuation_source": None,
        "founded_year": _founded_year(profile.get("founded_date")),
        "industry": ", ".join(sectors[:3]) if sectors else None,
    }
