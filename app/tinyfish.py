"""TinyFish Search API — free (no-credit) web search, used as a third,
best-effort source for `companies.industry` when FMP and StartupHub both
come back empty. See app.enrichment's docstring for why this pass is gated
to run only after those two.

Unlike FMP/StartupHub, TinyFish has no structured company-profile endpoint
-- it only returns ranked web search results (title/snippet/url), so an
"Industry: X" style fragment has to be extracted from raw text instead of
read off a dedicated field. Live testing against this project's own tracked
companies found real name-collision risk in that raw search step: "Georgia
IT" surfaced results about the US state's tech sector, not a company, and
"Qode" collided with at least three unrelated real businesses sharing that
name (a WordPress-theme shop, a Dubai PR agency, a Serbian dev team) -- the
same failure mode that already burned FMP once (Kong matched an unrelated
Beijing utility company).

Two safeguards mitigate this, both added per direct request:

1. Multi-source agreement (`_find_corroborated_industry`): an industry value
   is only accepted if a SECOND, independently-domained result corroborates
   it (e.g. LinkedIn AND ZoomInfo, never two results from the same domain --
   two paragraphs from the same page aren't independent evidence).
   Corroboration is substring containment on normalized text, not exact
   equality -- real sources phrase the same industry differently often
   enough (LinkedIn's "Construction" vs ZoomInfo's "Civil Engineering
   Construction, Construction") that requiring an exact match would reject
   most real, correct matches. This is deliberately conservative: a company
   with only one usable candidate, or two candidates that don't overlap at
   all, is left blank rather than guessed at -- same "not confident, don't
   guess" rule as every other enrichment source here.

2. Audit trail: the corroborating URL is returned alongside the value and
   stored in `companies.industry_source_url`, so a wrong match is visible
   and correctable by a human glance, not a silent black box -- same
   principle as the DOL LCA Match column.

Neither safeguard fully closes the collision gap: two independently-domained
results can still both be describing the same WRONG entity end-to-end (two
of the real Qode collision sources -- qodeinteractive.com and a LinkedIn
"Qode Themes" page -- are actually the same unrelated WordPress-theme
business, so they'd agree with each other while still being wrong for the
tracked company). This module doesn't attempt to verify company identity
beyond the search text itself; the audit URL is what lets a human catch that
specific residual risk.
"""
from __future__ import annotations

import re

from app.http_client import get_json

SEARCH_URL = "https://api.search.tinyfish.ai"

# Deliberately structured, not a loose keyword scan -- these only fire on
# the literal "Industry: X" / "Industry, X" style fragments that
# LinkedIn/Wikipedia/ZoomInfo infobox text gets indexed with verbatim. That
# specificity is itself a safeguard: a narrative sentence about an unrelated
# topic (e.g. "Georgia's IT sector sees 55% revenue growth") doesn't match
# any of these, so it's silently skipped rather than misread as a company's
# industry.
INDUSTRY_PATTERNS = [
    re.compile(r"Industry:\s*([A-Za-z][A-Za-z &,/\-]{1,60}?)(?:\s*;|\s*Company size|\s*\.|\n|$)"),
    re.compile(r"Industry,\s*([A-Za-z][A-Za-z &,/\-]{1,60}?)(?:\.\s*Founded|,\s*Founded|\.\s*Headquarters|\.|\n|$)"),
    re.compile(r"is in the industry of:\s*([A-Za-z][A-Za-z &,/\-]{1,80}?)(?:\.|$)"),
    re.compile(r"industry of:\s*([A-Za-z][A-Za-z &,/\-]{1,80}?)(?:\.|$|\s+What)"),
    re.compile(r"[Pp]rimary sector is\s*([A-Za-z][A-Za-z &,/\-]{1,60}?)\."),
]


def _domain(url: str) -> str:
    match = re.search(r"://(?:www\.)?([^/]+)", url or "")
    return match.group(1).lower() if match else ""


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().rstrip(".,;").lower()


def search_industry_candidates(company_name: str, api_key: str, session=None) -> list[tuple[str, str, str]]:
    """Returns (raw_industry_text, domain, url) for every search result
    whose title/snippet matches one of INDUSTRY_PATTERNS -- the raw
    candidate pool `_find_corroborated_industry` then checks for agreement
    across independent domains."""
    data = get_json(
        SEARCH_URL,
        params={"query": f"{company_name} industry", "location": "US", "language": "en"},
        headers={"X-API-Key": api_key},
        session=session,
    )
    candidates = []
    for result in data.get("results", []):
        text = f"{result.get('title', '')} {result.get('snippet', '')}"
        for pattern in INDUSTRY_PATTERNS:
            match = pattern.search(text)
            if match:
                url = result.get("url", "")
                candidates.append((match.group(1).strip(), _domain(url), url))
                break  # one candidate per result is enough
    return candidates


def _find_corroborated_industry(candidates: list[tuple[str, str, str]]) -> tuple[str, str] | None:
    """Returns (industry_text, source_url) for the first pair of candidates
    from two DIFFERENT, non-empty domains whose normalized text overlaps
    (one contains the other), or None if no two independent domains agree.
    Keeps whichever original candidate text is longer -- the more specific
    phrasing of the two agreeing candidates."""
    for i, (text_a, domain_a, url_a) in enumerate(candidates):
        if not domain_a:
            continue
        norm_a = _normalize(text_a)
        if not norm_a:
            continue
        for text_b, domain_b, url_b in candidates[i + 1:]:
            if not domain_b or domain_b == domain_a:
                continue
            norm_b = _normalize(text_b)
            if not norm_b:
                continue
            if norm_a in norm_b or norm_b in norm_a:
                return (text_a, url_a) if len(text_a) >= len(text_b) else (text_b, url_b)
    return None


def fetch_company_industry(company_name: str, api_key: str, session=None) -> dict | None:
    """Returns {"industry": ..., "industry_source_url": ...} only if
    corroborated by two independently-domained results (see module
    docstring) -- otherwise None, same "not confident, don't guess" contract
    every other enrichment source in this codebase follows."""
    candidates = search_industry_candidates(company_name, api_key, session=session)
    corroborated = _find_corroborated_industry(candidates)
    if corroborated is None:
        return None
    industry, source_url = corroborated
    return {"industry": industry, "industry_source_url": source_url}
