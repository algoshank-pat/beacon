from app.location_state import REMOTE_USA, resolve_location_state


def test_none_and_blank_resolve_to_none():
    assert resolve_location_state(None) is None
    assert resolve_location_state("") is None
    assert resolve_location_state("   ") is None


def test_city_state_abbreviation():
    assert resolve_location_state("Austin, TX") == "TX"
    assert resolve_location_state("Arlington, VA") == "VA"


def test_city_full_state_name():
    assert resolve_location_state("Anchorage, Alaska") == "AK"


def test_state_united_states():
    assert resolve_location_state("Arizona, United States") == "AZ"


def test_dc_aliases():
    assert resolve_location_state("Washington, DC") == "DC"
    assert resolve_location_state("Arlington County, Virginia") == "VA"


def test_unique_county_resolves_directly():
    assert resolve_location_state("Aberdeen Proving Ground, Harford County") == "MD"
    assert resolve_location_state("Alameda, Bernalillo County") == "NM"


def test_ambiguous_county_disambiguated_by_city():
    # "Alameda County" is unique to CA on its own, so this doesn't even need
    # disambiguation, but confirms the county branch takes priority.
    assert resolve_location_state("Alameda, Alameda County") == "CA"


def test_ambiguous_county_and_city_overlap_prefers_largest_population():
    # "Wayne County" exists in 16 states; "Detroit" exists in 5 (AL, IL, MI,
    # OR, TX). Both lists overlap on MI and IL -- MI's Detroit (633k) should
    # win over IL's Detroit (76).
    assert resolve_location_state("Detroit, Wayne County") == "MI"


def test_ambiguous_county_with_no_disambiguating_city_is_unresolved():
    assert resolve_location_state("Uptown, Marion County") is None


def test_bare_city_picks_most_populous_same_named_place():
    assert resolve_location_state("Austin") == "TX"
    assert resolve_location_state("Chicago") == "IL"
    assert resolve_location_state("Denver") == "CO"


def test_bare_city_with_informal_city_suffix():
    assert resolve_location_state("New York City") == "NY"
    # Real places whose actual name ends in "City" must resolve as-is, not
    # have "City" stripped off.
    assert resolve_location_state("Kansas City") == "MO"
    assert resolve_location_state("Oklahoma City") == "OK"


def test_us_dash_format():
    assert resolve_location_state("US-CA-Menlo Park") == "CA"
    assert resolve_location_state("US-CO-Denver") == "CO"


def test_remote_resolves_to_remote_usa():
    assert resolve_location_state("Remote") == REMOTE_USA
    assert resolve_location_state("Remote ") == REMOTE_USA
    assert resolve_location_state("Anywhere - Remote") == REMOTE_USA
    assert resolve_location_state("Remote (US)") == REMOTE_USA
    assert resolve_location_state("Remote - California") == REMOTE_USA


def test_bare_us_or_united_states_resolves_to_remote_usa():
    assert resolve_location_state("US") == REMOTE_USA
    assert resolve_location_state("USA") == REMOTE_USA
    assert resolve_location_state("United States") == REMOTE_USA


def test_us_dash_remote_resolves_to_remote_usa():
    assert resolve_location_state("US-CA-Remote") == REMOTE_USA


def test_nationwide_markers_resolve_to_remote_usa():
    assert resolve_location_state("*Job Posting Only: USA1") == REMOTE_USA
    assert resolve_location_state("Northeast - United States") == REMOTE_USA
    assert resolve_location_state("Central - United States") == REMOTE_USA


def test_multiple_distinct_states_resolve_to_remote_usa():
    assert resolve_location_state("Arizona; California; Utah") == REMOTE_USA
    assert (
        resolve_location_state(
            "Addison, TX (Hybrid); Bellevue, WA (Hybrid); Durham, NC (Hybrid)"
        )
        == REMOTE_USA
    )


def test_single_state_across_multiple_semicolon_parts_is_not_remote():
    assert resolve_location_state("New York, NY; Remote, USA; San Mateo, CA") == REMOTE_USA
    # Same state repeated across parts, no remote keyword -> that one state.
    assert resolve_location_state("Austin, TX; Dallas, TX") == "TX"


def test_non_us_remote_does_not_default_to_remote_usa():
    assert resolve_location_state("Remote - Estonia") is None
    assert resolve_location_state("Canada - Remote") is None


def test_unresolvable_strings_return_none():
    assert resolve_location_state("N/A") is None
    assert resolve_location_state("NAMER") is None
    assert resolve_location_state("Kansas City Metro") is None
