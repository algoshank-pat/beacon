from app.startuphub import fetch_company_profile, search_startup
from tests.fakes import FakeResponse, FakeSession

WORKATO_RESULT = {
    "name": "Workato",
    "slug": "workato",
    "hq_city": "Mountain View",
    "hq_country": "United States",
    "founded_date": "2013",
    "sectors": ["Integration Platform as a Service (iPaaS)", "Workflow Automation", "Enterprise Software"],
}


def test_search_startup_returns_exact_name_match():
    # regression case: the real API's `name=`/`query=`/`search=` params silently
    # ignore the filter and return an unrelated top result (confirmed live:
    # name=Workato returned Medtronic). This asserts search_startup requires
    # an exact (case-insensitive) name match, not just "first result".
    body = {"data": [{"name": "Medtronic", "slug": "medtronic"}, WORKATO_RESULT]}
    session = FakeSession([FakeResponse(200, body)])
    result = search_startup("Workato", "key", session=session)
    assert result["slug"] == "workato"


def test_search_startup_rejects_unrelated_top_result():
    body = {"data": [{"name": "Medtronic", "slug": "medtronic"}]}
    session = FakeSession([FakeResponse(200, body)])
    result = search_startup("Workato", "key", session=session)
    assert result is None


def test_search_startup_returns_none_for_empty_results():
    session = FakeSession([FakeResponse(200, {"data": []})])
    assert search_startup("GXM Technologies", "key", session=session) is None


def test_fetch_company_profile_normalizes_fields():
    session = FakeSession([FakeResponse(200, {"data": [WORKATO_RESULT]})])
    result = fetch_company_profile("Workato", "key", session=session)

    assert result["hq_location"] == "Mountain View, United States"
    assert result["founded_year"] == 2013
    assert "iPaaS" in result["industry"]
    # fields this tier can't provide must stay None, not fabricated
    assert result["employee_count"] is None
    assert result["funding_stage"] is None
    assert result["revenue_or_valuation"] is None


def test_fetch_company_profile_none_when_not_found():
    session = FakeSession([FakeResponse(200, {"data": []})])
    assert fetch_company_profile("Some Private Startup", "key", session=session) is None


def test_fetch_company_profile_handles_missing_hq_data():
    result_no_hq = dict(WORKATO_RESULT, hq_city=None, hq_country=None)
    session = FakeSession([FakeResponse(200, {"data": [result_no_hq]})])
    result = fetch_company_profile("Workato", "key", session=session)
    assert result["hq_location"] is None


def test_fetch_company_profile_parses_various_founded_date_formats():
    for raw, expected in [
        ("2013", 2013),
        ("2000-01-01", 2000),
        ("2019-01-01 00:00:00", 2019),
        (None, None),
    ]:
        result_variant = dict(WORKATO_RESULT, founded_date=raw)
        session = FakeSession([FakeResponse(200, {"data": [result_variant]})])
        result = fetch_company_profile("Workato", "key", session=session)
        assert result["founded_year"] == expected
