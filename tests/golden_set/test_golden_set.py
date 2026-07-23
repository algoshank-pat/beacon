"""Golden test set — verified expected visa_flag for a mix of real (ingested)
and constructed job descriptions. Regex-confident cases run in the normal fast
suite (no API). Cases that require the Haiku fallback hit the real API and are
skipped by default -- opt in with RUN_GOLDEN_LIVE=1 (costs a small amount and
needs ANTHROPIC_API_KEY set) after changing HAIKU_PROMPT or regex patterns.

real-151-germany-only-restricted is the flagship case: it caught two real bugs
during development (a 4000-char prompt truncation that cut off the relevant
sentence entirely, and a missing job-location context that caused Haiku to
misread "we sponsor visas to Germany" as a positive US signal on a US posting).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.visa_scan import haiku_classify, regex_classify

CASES_PATH = Path(__file__).parent / "cases.json"
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))

REGEX_CASES = [c for c in CASES if c["regex_confident"]]
HAIKU_CASES = [c for c in CASES if not c["regex_confident"]]


@pytest.mark.parametrize("case", REGEX_CASES, ids=[c["id"] for c in REGEX_CASES])
def test_regex_confident_golden_cases(case):
    flag, snippet = regex_classify(case["description"])
    assert flag == case["expected_visa_flag"], (
        f"{case['id']}: expected {case['expected_visa_flag']!r}, got {flag!r} ({case['notes']})"
    )


@pytest.mark.skipif(
    os.environ.get("RUN_GOLDEN_LIVE") != "1",
    reason="Hits the real Anthropic API -- set RUN_GOLDEN_LIVE=1 to run (e.g. after changing HAIKU_PROMPT).",
)
@pytest.mark.parametrize("case", HAIKU_CASES, ids=[c["id"] for c in HAIKU_CASES])
def test_haiku_golden_cases_live(case):
    import anthropic

    from app.config import get_settings

    client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key)

    # Confirm these cases actually need Haiku (regex should return ambiguous).
    regex_flag, _ = regex_classify(case["description"])
    assert regex_flag is None, f"{case['id']}: expected regex to be ambiguous, got {regex_flag!r}"

    result, _usage = haiku_classify(
        client, case["description"], title=case.get("title", ""), location=case.get("location", "")
    )
    assert result["visa_flag"] == case["expected_visa_flag"], (
        f"{case['id']}: expected {case['expected_visa_flag']!r}, got {result!r} ({case['notes']})"
    )
