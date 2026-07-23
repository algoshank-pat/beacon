"""Resolves a job's free-text `location` string (as scraped from Greenhouse/
Lever/Ashby/Adzuna) down to a single US state abbreviation, or "Remote-USA"
for remote/nationwide/multi-state postings.

The raw location strings are inconsistent across sources: "City, ST",
"City, State Name", "State, United States", "City, County Name" (no state
at all -- the case that actually prompted this module, since a county name
alone doesn't determine a state; e.g. "Washington County" exists in ~30
states), bare city names with no state ("Austin", "Denver"), semicolon/
slash-separated multi-location postings, and various "Remote"/"Hybrid"/
"Anywhere" phrasings.

Resolution order per location "part" (parts are split on ';' and '/', since
multi-office postings use either): explicit state name/abbreviation token >
county-name token (disambiguated by a sibling city token when the county
name isn't unique to one state) > bare city name, resolved via the most
populous same-named place nationwide (a deliberate best-guess -- reading
"Austin" with no other context, the overwhelmingly likely answer is
Austin, TX, not one of the ~4,000-person towns sharing the name). Genuinely
ambiguous county cases (same county name in multiple states, sibling token
doesn't disambiguate) are left unresolved (None) rather than guessed.

If any part signals "remote"/"anywhere"/"nationwide", or multiple parts
resolve to different states, the whole location is "Remote-USA". A handful
of explicit non-US markers (country/region names) suppress that fallback
for remote postings that are clearly not US-based.

Reference data (`app/data/us_counties.csv`, `app/data/us_places.csv`) is
derived from public-domain US Census Bureau files (2020 national county
list; 2023 population estimates for incorporated places/CDPs) -- not
scraped from any job board, just a static geography lookup.
"""
from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path

_DATA_DIR = Path(__file__).parent / "data"

STATE_NAME_TO_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}
_STATE_NAME_TO_ABBR_LOWER = {name.lower(): abbr for name, abbr in STATE_NAME_TO_ABBR.items()}
_STATE_ABBR_SET = set(STATE_NAME_TO_ABBR.values())
_STATE_ALIASES = {
    "dc": "DC", "d.c": "DC", "washington dc": "DC", "washington d.c": "DC",
}

REMOTE_USA = "Remote-USA"

_PAREN_RE = re.compile(r"\([^)]*\)")
_NOISE_RE = re.compile(r"\b(remote|hybrid|onsite|on-site|anywhere)\b[\s:\-]*", re.IGNORECASE)
_REMOTE_RE = re.compile(r"\b(remote|anywhere)\b", re.IGNORECASE)
_NATIONWIDE_RE = re.compile(
    r"usa1|us[\s-]?wide|nationwide|\*job posting only|"
    r"\b(northeast|southeast|southwest|northwest|midwest|central)\b",
    re.IGNORECASE,
)
# A location that, once cleaned, is nothing but "US"/"USA"/"United States" --
# no specific state at all -- means "hire anywhere in the US".
_BARE_US_RE = re.compile(r"^u\.?s\.?a?\.?$|^united states(\s+of\s+america)?$", re.IGNORECASE)
# Some ATS boards format US locations as "US-CA-Menlo Park" / "US-CO-Denver".
_DASH_US_RE = re.compile(r"^US-([A-Za-z]{2})-(.+)$", re.IGNORECASE)
# Explicit non-US signals -- suppresses the Remote-USA fallback for postings
# that are clearly remote-but-not-US (e.g. "Remote - Estonia", "Canada - Remote").
_NON_US_MARKERS = {
    "canada", "ontario", "europe", "estonia", "uk", "united kingdom", "india",
    "germany", "poland", "australia", "philippines", "mexico", "brazil",
    "argentina", "singapore", "ireland", "spain", "france", "netherlands",
    "colombia", "japan", "china", "israel", "portugal", "italy", "sweden",
    "switzerland", "austria", "belgium", "denmark", "norway", "finland",
    "new zealand", "south africa", "romania", "ukraine",
}


@lru_cache(maxsize=1)
def _county_to_states() -> dict[str, frozenset[str]]:
    mapping: dict[str, set[str]] = {}
    with open(_DATA_DIR / "us_counties.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            mapping.setdefault(row["county_name"].lower(), set()).add(row["state"])
    return {name: frozenset(states) for name, states in mapping.items()}


@lru_cache(maxsize=1)
def _place_to_states() -> dict[str, tuple[tuple[str, int], ...]]:
    mapping: dict[str, list[tuple[str, int]]] = {}
    with open(_DATA_DIR / "us_places.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            mapping.setdefault(row["place_name"].lower(), []).append(
                (row["state"], int(row["population"]))
            )
    return {name: tuple(entries) for name, entries in mapping.items()}


def _place_entries(
    place_to_states: dict[str, tuple[tuple[str, int], ...]], token: str
) -> tuple[tuple[str, int], ...]:
    """Looks up a token as a place name, trying the literal token first, then
    (only if that has no match) with an informal trailing "City" stripped --
    handles "New York City" -> "New York" without breaking real "X City"
    places like "Kansas City" or "Oklahoma City", which already match as-is."""
    entries = place_to_states.get(token.lower(), ())
    if entries:
        return entries
    if token.lower().endswith(" city"):
        return place_to_states.get(token[: -len(" city")].lower(), ())
    return ()


def _match_state_token(token: str) -> str | None:
    t = token.strip().rstrip(".")
    if not t:
        return None
    if t.upper() in _STATE_ABBR_SET:
        return t.upper()
    lower = t.lower()
    if lower in _STATE_ALIASES:
        return _STATE_ALIASES[lower]
    return _STATE_NAME_TO_ABBR_LOWER.get(lower)


def _clean_part(part: str) -> str:
    cleaned = _PAREN_RE.sub("", part)
    cleaned = _NOISE_RE.sub("", cleaned)
    return cleaned.strip(" ,-\t")


def _resolve_part(part: str) -> str | None:
    cleaned = _clean_part(part)
    if not cleaned:
        return None

    dash_match = _DASH_US_RE.match(cleaned)
    if dash_match and dash_match.group(1).upper() in _STATE_ABBR_SET:
        return dash_match.group(1).upper()

    tokens = [t.strip() for t in cleaned.split(",") if t.strip()]
    if not tokens:
        return None

    # An explicit state name/abbreviation anywhere in the part wins outright.
    for token in reversed(tokens):
        abbr = _match_state_token(token)
        if abbr:
            return abbr

    # A county name determines the state directly if it's unique nationwide;
    # otherwise a sibling token (the city part) narrows it down. If more than
    # one state has both that county name AND a same-named place (e.g. both
    # IL and MI have a "Wayne County" and a small place called "Detroit"),
    # prefer whichever candidate's place population is largest -- the same
    # "assume the well-known one" heuristic as the bare-city fallback below.
    county_to_states = _county_to_states()
    for i, token in enumerate(tokens):
        county_states = county_to_states.get(token.lower())
        if not county_states:
            continue
        if len(county_states) == 1:
            return next(iter(county_states))
        place_to_states = _place_to_states()
        for other_token in tokens[:i] + tokens[i + 1:]:
            overlap_entries = [
                entry for entry in _place_entries(place_to_states, other_token)
                if entry[0] in county_states
            ]
            if overlap_entries:
                return max(overlap_entries, key=lambda entry: entry[1])[0]
        return None  # county name is ambiguous and no sibling token resolves it

    # Bare city name(s), no state or county signal at all -- best-guess using
    # the most populous same-named place across every token in this part.
    place_to_states = _place_to_states()
    candidates = [entry for token in tokens for entry in _place_entries(place_to_states, token)]
    if candidates:
        return max(candidates, key=lambda entry: entry[1])[0]

    return None


def resolve_location_state(location: str | None) -> str | None:
    """Returns a 2-letter state abbreviation, REMOTE_USA, or None (couldn't
    confidently resolve -- left blank rather than guessed)."""
    if not location or not location.strip():
        return None
    text = location.strip()
    lower = text.lower()

    parts = re.split(r"[;/]", text)
    resolved_states = {state for part in parts if (state := _resolve_part(part))}
    saw_remote = bool(_REMOTE_RE.search(lower))

    if resolved_states:
        if saw_remote or len(resolved_states) > 1:
            return REMOTE_USA
        return next(iter(resolved_states))

    if saw_remote:
        if any(re.search(rf"\b{re.escape(marker)}\b", lower) for marker in _NON_US_MARKERS):
            return None
        return REMOTE_USA

    if _NATIONWIDE_RE.search(lower):
        return REMOTE_USA

    if _BARE_US_RE.match(text.strip().rstrip(".")):
        return REMOTE_USA

    return None
