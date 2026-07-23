"""Heuristic US-location detection over free-text location strings.

This is not a geocoder — it's a fast, dependency-free heuristic tuned against
the location formats actually seen from Adzuna/Greenhouse/Lever/Ashby ("City,
State", "City, Country", bare country/city names, region codes like APAC/EMEA,
semicolon-separated multi-location lists). It's deliberately fail-open: a
segment with no recognizable signal either way is treated as unknown, not
excluded, so it never silently drops a job over an unfamiliar location string.
A location with ANY confirmed-US segment passes (a US option exists); a
location with a confirmed non-US segment and no US segment fails.
"""
from __future__ import annotations

import re

US_STATE_ABBREVIATIONS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
}

# Countries/regions and specific non-US cities that show up (or plausibly could)
# in real job-board location strings. "Georgia", "San Jose", "Manchester",
# "Melbourne", and "Vancouver" are deliberately NOT here even though they're
# also non-US place names elsewhere (country of Georgia, San Jose Costa Rica,
# Manchester NH, Melbourne FL, Vancouver WA are all real US places) -- they'd
# cause false exclusions more often than they'd catch a real non-US posting.
# Rely on more specific compound signals instead (see NON_US_COMPOUND_SIGNALS).
NON_US_SIGNALS = [
    "philippines", "spain", "united kingdom", "netherlands", "sweden", "india",
    "israel", "australia", "south korea", "korea", "canada", "mexico",
    "bulgaria", "cyprus", "serbia", "germany", "portugal", "greece",
    "costa rica", "singapore", "france", "italy", "poland", "romania",
    "ireland", "japan", "china", "brazil", "argentina", "colombia", "chile",
    "peru", "vietnam", "thailand", "indonesia", "malaysia", "new zealand",
    "switzerland", "austria", "belgium", "denmark", "norway", "finland",
    "czech", "hungary", "ukraine", "russia", "turkey", "egypt",
    "south africa", "nigeria", "kenya", "uae", "united arab emirates",
    "saudi arabia", "pakistan", "bangladesh", "sri lanka", "taiwan",
    "hong kong", "apac", "emea", "latam", "cambodia", "uruguay", "croatia",
    "luxembourg", "qatar",
    "tbilisi", "alabang", "barcelona", "amsterdam", "stockholm",
    "hyderabad", "tel aviv", "sydney", "seoul", "toronto", "mexico city",
    "bangalore", "manila", "sofia", "nicosia",
    "porto", "guadalajara", "monterrey", "belgrade", "yerevan", "frankfurt",
    "munich", "bengaluru", "chennai", "mumbai", "pune",
    "gurgaon", "gurugram", "jaipur", "tokyo", "jakarta", "shanghai", "beijing",
    "zurich", "tallinn", "reykjavik", "sao paulo",
    "bucharest", "herzliya", "phnom penh", "escazu", "montreal", "quebec",
    "british columbia", "dubai", "doha", "riyadh", "dusseldorf", "zagreb",
    "split, hrv",
]

# Compound (not bare) signals, used only when checked verbatim against the
# whole lowered segment -- these cities have real, common US namesakes
# (Manchester NH/CT, Melbourne FL, Geneva NY/IL), so a bare city name would
# cause false exclusions; but the specific country/region-code pairing seen
# in real postings is unambiguous.
NON_US_COMPOUND_SIGNALS = [
    "manchester, uk", "au-melbourne", "geneva, ge",
]

# Bare non-US city names with a real, plausible US namesake town (Paris, TX/
# TN/KY/ME; Dublin, OH/GA/CA; London, OH/KY; Berlin, NH/CT; Warsaw, IN/NY;
# Madrid, NY/IA; Lisbon, OH/CT). Checked only AFTER confirming the segment
# isn't a US "City, ST"/"City, County"/"City, State" pattern (see
# _segment_is_us) -- real bug found live: "South Paris, Oxford County" (a
# real Maine town) was misclassified as Paris, France, because "paris" was
# in the unconditional NON_US_SIGNALS list checked before any US signal.
NON_US_RISKY_SIGNALS = [
    "paris", "dublin", "london", "berlin", "warsaw", "madrid", "lisbon",
]

# Real job-board location strings from global companies frequently encode
# office location as an ISO-3166 country code prefix, e.g. "AE-Dubai",
# "IN-Bengaluru", "JP-Tokyo", "CH-Zurich-MSO" -- catches this whole family
# even for city names not individually listed above. Deliberately excludes
# "us"/"ca" (Canada's code collides with nothing here, but "ca" is also
# sometimes used loosely for California) to avoid a new ambiguity.
NON_US_COUNTRY_CODE_PREFIXES = {
    "ae", "au", "br", "ch", "cn", "de", "fr", "gb", "in", "jp", "no", "pl",
    "sa", "se", "nl", "es", "it", "ie", "kr", "sg", "mx",
}

_US_TOKEN_RE = re.compile(r"\b(united states|usa|u\.s\.a?\.?)\b", re.IGNORECASE)
_STATE_ABBR_RE = re.compile(r",\s*([A-Za-z]{2})\b")
_COUNTRY_CODE_PREFIX_RE = re.compile(r"^([a-zA-Z]{2})-")
_US_COUNTY_PARISH_RE = re.compile(r"\b(county|parish)\b", re.IGNORECASE)


def _is_confirmed_us(segment: str, lowered: str) -> bool:
    if _US_TOKEN_RE.search(segment):
        return True
    if _US_COUNTY_PARISH_RE.search(segment):
        return True
    for state in US_STATE_NAMES:
        if state in lowered:
            return True
    for match in _STATE_ABBR_RE.finditer(segment):
        if match.group(1).upper() in US_STATE_ABBREVIATIONS:
            return True
    return False


def _segment_is_us(segment: str) -> bool | None:
    """True = confirmed US, False = confirmed non-US, None = unrecognized.

    Checks unambiguous non-US signals (no real US namesake, e.g. "herzliya")
    first, then a US confirmation (state abbreviation, state name, "United
    States"/"USA", or a "County"/"Parish" administrative suffix -- Adzuna's
    real US-sourced job locations always end in one of these), and only
    THEN the "risky" city names that also have a real US namesake town
    (Paris, Dublin, London, ...). This ordering matters both ways: "Herzliya,
    IL" must resolve as Israel even though "IL" is also Illinois' state code
    (unambiguous non-US signal wins), while "South Paris, Oxford County"
    must resolve as US despite containing "Paris" (US confirmation wins over
    the risky-signal list). A single check order can't satisfy both."""
    lowered = segment.lower()

    for signal in NON_US_SIGNALS:
        if signal in lowered:
            return False

    for signal in NON_US_COMPOUND_SIGNALS:
        if signal in lowered:
            return False

    if _is_confirmed_us(segment, lowered):
        return True

    for signal in NON_US_RISKY_SIGNALS:
        if signal in lowered:
            return False

    prefix_match = _COUNTRY_CODE_PREFIX_RE.match(segment)
    if prefix_match and prefix_match.group(1).lower() in NON_US_COUNTRY_CODE_PREFIXES:
        return False

    return None


def is_us_location(location: str | None) -> bool:
    """A location with no segments recognized either way passes (fail-open)."""
    if not location:
        return True

    segments = [s.strip() for s in location.split(";") if s.strip()]
    results = [_segment_is_us(s) for s in segments]

    if any(r is True for r in results):
        return True
    if any(r is False for r in results):
        return False
    return True
