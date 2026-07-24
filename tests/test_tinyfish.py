from app.tinyfish import (
    _find_corroborated_industry,
    fetch_company_industry,
    search_industry_candidates,
)
from tests.fakes import FakeResponse, FakeSession


def _search_body(results):
    return {"query": "irrelevant", "results": results}


def test_search_industry_candidates_extracts_linkedin_style_colon_pattern():
    results = [
        {
            "position": 1, "site_name": "www.linkedin.com", "url": "https://www.linkedin.com/company/infoobjects",
            "title": "InfoObjects Inc.",
            "snippet": "Industry: IT Services and IT Consulting ; Company size: 201-500 employees",
        },
    ]
    session = FakeSession([FakeResponse(200, _search_body(results))])
    candidates = search_industry_candidates("InfoObjects Inc", "key", session=session)
    assert candidates == [("IT Services and IT Consulting", "linkedin.com", "https://www.linkedin.com/company/infoobjects")]


def test_search_industry_candidates_extracts_wikipedia_style_comma_pattern():
    results = [
        {
            "position": 1, "site_name": "en.wikipedia.org", "url": "https://en.wikipedia.org/wiki/Collabera",
            "title": "Collabera",
            "snippet": "Collabera Inc is a company. Industry, Information Technology. Founded, 1991.",
        },
    ]
    session = FakeSession([FakeResponse(200, _search_body(results))])
    candidates = search_industry_candidates("Collabera", "key", session=session)
    assert candidates == [("Information Technology", "en.wikipedia.org", "https://en.wikipedia.org/wiki/Collabera")]


def test_search_industry_candidates_extracts_zoominfo_style_phrase():
    results = [
        {
            "position": 1, "site_name": "www.zoominfo.com", "url": "https://www.zoominfo.com/c/infoobjects",
            "title": "InfoObjects - Overview",
            "snippet": "InfoObjects is in the industry of: Business Services, Software Testing",
        },
    ]
    session = FakeSession([FakeResponse(200, _search_body(results))])
    candidates = search_industry_candidates("InfoObjects", "key", session=session)
    assert candidates == [("Business Services, Software Testing", "zoominfo.com", "https://www.zoominfo.com/c/infoobjects")]


def test_search_industry_candidates_ignores_narrative_text_with_no_structured_pattern():
    # Real regression case: "Georgia IT" surfaced narrative results about the
    # US state's tech sector, not a company -- none of these contain a
    # literal "Industry:"-style fragment, so nothing should be extracted.
    results = [
        {
            "position": 1, "site_name": "georgia.org", "url": "https://georgia.org/industries",
            "title": "Georgia's Industries",
            "snippet": "Robust digital infrastructure, cybersecurity expertise, and skilled talent support 260+ fintechs.",
        },
        {
            "position": 2, "site_name": "www.linkedin.com", "url": "https://www.linkedin.com/posts/some-post",
            "title": "Georgia's IT sector sees 55% revenue growth in H1 2025",
            "snippet": "Georgia is a major IT growth hub! Export Revenue: $489 million.",
        },
    ]
    session = FakeSession([FakeResponse(200, _search_body(results))])
    candidates = search_industry_candidates("Georgia IT", "key", session=session)
    assert candidates == []


def test_search_industry_candidates_skips_results_with_no_snippet_or_title():
    results = [{"position": 1, "site_name": "example.com", "url": "https://example.com"}]
    session = FakeSession([FakeResponse(200, _search_body(results))])
    assert search_industry_candidates("Some Co", "key", session=session) == []


# --- _find_corroborated_industry ---


def test_find_corroborated_industry_accepts_overlapping_text_from_different_domains():
    # Real case: LinkedIn's "Construction" is a substring of ZoomInfo's more
    # specific "Civil Engineering Construction, Construction" -- different
    # phrasing, same real industry, corroborated across two independent
    # domains.
    candidates = [
        ("Construction", "linkedin.com", "https://linkedin.com/a"),
        ("Civil Engineering Construction, Construction", "zoominfo.com", "https://zoominfo.com/b"),
    ]
    result = _find_corroborated_industry(candidates)
    assert result == ("Civil Engineering Construction, Construction", "https://zoominfo.com/b")


def test_find_corroborated_industry_rejects_a_single_domain_only():
    candidates = [("IT Services", "linkedin.com", "https://linkedin.com/a")]
    assert _find_corroborated_industry(candidates) is None


def test_find_corroborated_industry_rejects_two_candidates_from_the_same_domain():
    # Two paragraphs from the same page are not independent evidence.
    candidates = [
        ("IT Services", "linkedin.com", "https://linkedin.com/a"),
        ("IT Services and Consulting", "linkedin.com", "https://linkedin.com/a"),
    ]
    assert _find_corroborated_industry(candidates) is None


def test_find_corroborated_industry_rejects_when_independent_domains_disagree():
    # Real regression case: "Qode" collided with unrelated businesses whose
    # descriptions genuinely don't overlap -- a WordPress-theme shop and a
    # Dubai luxury PR agency. No overlap between independent domains means
    # no corroboration, so this must stay unmatched rather than force-picking
    # one.
    candidates = [
        ("WordPress theme and template business", "qodeinteractive.com", "https://qodeinteractive.com"),
        ("premium luxury PR and events agency", "zoominfo.com", "https://zoominfo.com/the-qode"),
    ]
    assert _find_corroborated_industry(candidates) is None


def test_find_corroborated_industry_ignores_candidates_with_no_domain():
    candidates = [
        ("IT Services", "", "not-a-real-url"),
        ("IT Services", "linkedin.com", "https://linkedin.com/a"),
    ]
    assert _find_corroborated_industry(candidates) is None


# --- fetch_company_industry ---


def test_fetch_company_industry_returns_corroborated_match():
    results = [
        {
            "position": 1, "site_name": "www.linkedin.com", "url": "https://linkedin.com/company/solutionit",
            "title": "Solution IT, Inc.", "snippet": "Industry: Information Technology & Services ; Company size: 201-500",
        },
        {
            "position": 2, "site_name": "www.zoominfo.com", "url": "https://zoominfo.com/solutionit",
            "title": "SolutionIT Overview",
            "snippet": "SolutionIT is in the industry of: Information Technology & Services, IT Staffing",
        },
    ]
    session = FakeSession([FakeResponse(200, _search_body(results))])
    result = fetch_company_industry("SolutionIT", "key", session=session)
    assert result == {
        "industry": "Information Technology & Services, IT Staffing",
        "industry_source_url": "https://zoominfo.com/solutionit",
    }


def test_fetch_company_industry_returns_none_when_uncorroborated():
    results = [
        {
            "position": 1, "site_name": "www.linkedin.com", "url": "https://linkedin.com/company/x",
            "title": "X", "snippet": "Industry: Information Technology & Services",
        },
    ]
    session = FakeSession([FakeResponse(200, _search_body(results))])
    assert fetch_company_industry("X", "key", session=session) is None


def test_fetch_company_industry_returns_none_for_empty_results():
    session = FakeSession([FakeResponse(200, _search_body([]))])
    assert fetch_company_industry("Nobody Inc", "key", session=session) is None
