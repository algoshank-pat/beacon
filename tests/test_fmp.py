from app.fmp import fetch_company_profile, get_profile, search_symbol
from tests.fakes import FakeResponse, FakeSession

TWLO_PROFILE = {
    "symbol": "TWLO",
    "companyName": "Twilio Inc.",
    "marketCap": 31767786637,
    "fullTimeEmployees": "5502",
    "city": "San Francisco",
    "state": "CA",
    "country": "US",
    "isActivelyTrading": True,
}


def test_search_symbol_prefers_us_exchange():
    results = [
        {"symbol": "0LHL.L", "exchange": "LSE", "name": "Twilio Inc."},
        {"symbol": "TWLO", "exchange": "NYSE", "name": "Twilio Inc."},
    ]
    session = FakeSession([FakeResponse(200, results)])
    assert search_symbol("Twilio", "key", session=session) == "TWLO"


def test_search_symbol_returns_none_for_no_results():
    session = FakeSession([FakeResponse(200, [])])
    assert search_symbol("Some Private Startup", "key", session=session) is None


def test_search_symbol_falls_back_to_first_result_when_no_preferred_exchange():
    results = [{"symbol": "MTLA.MI", "exchange": "MIL", "name": "Something Corp"}]
    session = FakeSession([FakeResponse(200, results)])
    assert search_symbol("Something", "key", session=session) == "MTLA.MI"


def test_search_symbol_rejects_unrelated_name_match():
    # Confirmed live: querying "Saic" (a real public defense contractor,
    # ticker SAIC) returned Mosaic's ticker instead -- FMP's fuzzy name
    # search can surface a completely unrelated top result for short/
    # generic company names.
    results = [{"symbol": "MOS", "exchange": "NYSE", "name": "The Mosaic Company"}]
    session = FakeSession([FakeResponse(200, results)])
    assert search_symbol("Saic", "key", session=session) is None


def test_search_symbol_skips_unrelated_result_and_uses_a_verified_one():
    results = [
        {"symbol": "XYZ", "exchange": "NYSE", "name": "Unrelated Micro-Cap Inc."},
        {"symbol": "BAESY", "exchange": "OTC", "name": "BAE Systems plc"},
    ]
    session = FakeSession([FakeResponse(200, results)])
    assert search_symbol("BAE Systems", "key", session=session) == "BAESY"


def test_search_symbol_tolerates_corporate_suffix_differences():
    results = [{"symbol": "CMI", "exchange": "NYSE", "name": "Cummins Inc."}]
    session = FakeSession([FakeResponse(200, results)])
    assert search_symbol("Cummins", "key", session=session) == "CMI"


def test_get_profile_returns_first_result():
    session = FakeSession([FakeResponse(200, [TWLO_PROFILE])])
    assert get_profile("TWLO", "key", session=session) == TWLO_PROFILE


def test_get_profile_returns_none_when_empty():
    session = FakeSession([FakeResponse(200, [])])
    assert get_profile("ZZZZ", "key", session=session) is None


def test_fetch_company_profile_normalizes_fields():
    session = FakeSession(
        [
            FakeResponse(200, [{"symbol": "TWLO", "exchange": "NYSE", "name": "Twilio Inc."}]),
            FakeResponse(200, [TWLO_PROFILE]),
        ]
    )
    result = fetch_company_profile("Twilio", "key", session=session)

    assert result["employee_count"] == 5502
    assert result["hq_location"] == "San Francisco, CA"
    assert result["company_type"] == "public"
    assert result["funding_stage"] == "ipo_public"
    assert result["revenue_or_valuation"] == "$31,767,786,637 market cap"
    assert "TWLO" in result["revenue_valuation_source"]
    assert result["founded_year"] is None


def test_fetch_company_profile_returns_none_for_unlisted_company():
    session = FakeSession([FakeResponse(200, [])])  # search returns nothing -- private company
    result = fetch_company_profile("Some Private Startup", "key", session=session)
    assert result is None
