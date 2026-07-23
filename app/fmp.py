"""Financial Modeling Prep — free, structured company profile data for
PUBLICLY TRADED companies only (their profile endpoint requires a stock
ticker symbol; private companies simply don't resolve to one). Used as the
cheap first choice in enrichment; private companies fall back to the
web-search + Sonnet path in app/enrichment.py."""
from __future__ import annotations

import re

from app.http_client import get_json

SEARCH_URL = "https://financialmodelingprep.com/stable/search-name"
PROFILE_URL = "https://financialmodelingprep.com/stable/profile"

# When a company name resolves to multiple listings (e.g. dual-listed on NYSE
# and LSE), prefer the primary US exchange listing.
_PREFERRED_EXCHANGES = ("NASDAQ", "NYSE")

# Stripped before comparing names so "Cummins" still matches FMP's registered
# "Cummins Inc." -- these differ inconsistently between our seed list and
# FMP's legal names, and shouldn't count as a mismatch.
_CORP_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "llc", "ltd", "limited",
    "co", "company", "plc", "group", "holdings", "holding",
}


def _normalize_company_name(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return [w for w in words if w not in _CORP_SUFFIXES]


def _names_plausibly_match(query: str, candidate: str) -> bool:
    """Guards against FMP's fuzzy name search returning an unrelated
    company for short/generic queries -- confirmed live at scale: "Saic"
    (a real public defense contractor, ticker SAIC) matched Mosaic's ticker
    instead, "Bloomberg" matched an unrelated gold-mining stock, "Google"
    matched an unrelated micro-cap. This never surfaced before because so
    few companies had ever gone through FMP; it corrupted company_type/
    financial data for ~10 companies once the FMP pass started running at
    real volume. Requires every significant word of whichever name is
    shorter to appear as a whole word in the other, after stripping common
    corporate suffixes -- strict enough to reject the false positives above,
    permissive enough to still match "Twilio" -> "Twilio Inc.". Deliberately
    conservative: an ambiguous short name (e.g. "Sentinel" not exactly
    matching "SentinelOne, Inc.") is rejected rather than guessed at, in
    keeping with this pipeline's "leave it blank rather than risk being
    wrong" policy for enrichment."""
    query_words = _normalize_company_name(query)
    candidate_words = _normalize_company_name(candidate)
    if not query_words or not candidate_words:
        return False
    shorter, longer = (
        (query_words, candidate_words) if len(query_words) <= len(candidate_words) else (candidate_words, query_words)
    )
    longer_set = set(longer)
    return all(word in longer_set for word in shorter)


def search_symbol(company_name: str, api_key: str, session=None) -> str | None:
    results = get_json(SEARCH_URL, params={"query": company_name, "apikey": api_key}, session=session)
    if not results:
        return None

    verified = [r for r in results if _names_plausibly_match(company_name, r.get("name", ""))]
    if not verified:
        return None

    for exchange in _PREFERRED_EXCHANGES:
        for result in verified:
            if result.get("exchange") == exchange:
                return result["symbol"]

    return verified[0]["symbol"]


def get_profile(symbol: str, api_key: str, session=None) -> dict | None:
    results = get_json(PROFILE_URL, params={"symbol": symbol, "apikey": api_key}, session=session)
    if not results:
        return None
    return results[0]


def _to_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _hq_location(profile: dict) -> str | None:
    city, state, country = profile.get("city"), profile.get("state"), profile.get("country")
    if city and state:
        return f"{city}, {state}"
    if city and country:
        return f"{city}, {country}"
    return city or state or country or None


def fetch_company_profile(company_name: str, api_key: str, session=None) -> dict | None:
    """Returns a dict shaped for the companies table, or None if this company
    isn't publicly traded (not found in FMP -- the expected, common case)."""
    symbol = search_symbol(company_name, api_key, session=session)
    if symbol is None:
        return None

    profile = get_profile(symbol, api_key, session=session)
    if profile is None:
        return None

    market_cap = profile.get("marketCap")
    revenue_or_valuation = f"${market_cap:,.0f} market cap" if market_cap else None

    return {
        "employee_count": _to_int(profile.get("fullTimeEmployees")),
        "employee_count_range": None,
        "hq_location": _hq_location(profile),
        "company_type": "public",
        "funding_stage": "ipo_public",
        "revenue_or_valuation": revenue_or_valuation,
        "revenue_valuation_source": f"Financial Modeling Prep (symbol: {symbol})",
        "founded_year": None,  # not exposed as a structured field by this endpoint
        "industry": profile.get("industry"),
    }
