from app.salary_extraction import extract_salary_range, resolve_posted_salary
from tests.fakes import FakeResponse, FakeSession

REAL_CARGILL_TEXT = (
    "more of relevant experience. #HiPo Compensation Data "
    "The expected salary for this position is $140,000 - $200,000. Compensation varies "
    "depending on a wide array of factors."
)


def test_extract_salary_range_matches_real_posting_text():
    assert extract_salary_range(REAL_CARGILL_TEXT) == (140000, 200000)


def test_extract_salary_range_handles_en_dash():
    assert extract_salary_range("Salary: $140,000 – $200,000 annually") == (140000, 200000)


def test_extract_salary_range_handles_to_separator():
    assert extract_salary_range("Pay range: $140,000 to $200,000") == (140000, 200000)


def test_extract_salary_range_handles_k_shorthand():
    assert extract_salary_range("Compensation: $140K - $200K DOE") == (140000, 200000)


def test_extract_salary_range_handles_missing_second_dollar_sign():
    assert extract_salary_range("$140,000 - 200,000 per year") == (140000, 200000)


def test_extract_salary_range_swaps_if_reversed():
    assert extract_salary_range("$200,000 - $140,000") == (140000, 200000)


def test_extract_salary_range_rejects_implausible_values():
    # not a salary -- a phone number / zip-like pattern that happens to match loosely
    assert extract_salary_range("Call us at $1,234 - $5,678,901 for details") == (None, None)


def test_extract_salary_range_none_when_absent():
    assert extract_salary_range("This role focuses on integration architecture.") == (None, None)


def test_extract_salary_range_none_for_empty_or_none():
    assert extract_salary_range(None) == (None, None)
    assert extract_salary_range("") == (None, None)


def test_extract_salary_range_ignores_valuation_figures():
    # company valuations like "$5.7B" shouldn't false-positive against the digit-group pattern
    assert extract_salary_range("Backed by a $5.7B valuation as of 2021") == (None, None)


def test_resolve_posted_salary_uses_description_first_no_network():
    session = FakeSession([])  # would raise IndexError if a network call were attempted
    result = resolve_posted_salary(REAL_CARGILL_TEXT, "https://example.com/job/1", session=session)
    assert result == (140000, 200000)
    assert session.calls == []


def test_resolve_posted_salary_falls_back_to_fetching_full_page():
    short_description = "Cargill is hiring a Principal Solutions Architect."  # no salary here
    page_html = "<p>Compensation Data</p><p>The expected salary is $140,000 - $200,000.</p>"
    session = FakeSession([FakeResponse(200, text=page_html)])

    result = resolve_posted_salary(short_description, "https://example.com/job/1", session=session)
    assert result == (140000, 200000)
    assert len(session.calls) == 1


def test_resolve_posted_salary_returns_none_when_fetch_fails():
    session = FakeSession([FakeResponse(403, text="blocked")])
    result = resolve_posted_salary("no salary here", "https://example.com/job/1", session=session)
    assert result == (None, None)


def test_resolve_posted_salary_returns_none_without_url():
    result = resolve_posted_salary("no salary here", None)
    assert result == (None, None)
