from app.location import is_us_location


def test_none_or_empty_passes_permissively():
    assert is_us_location(None) is True
    assert is_us_location("") is True


def test_explicit_united_states_passes():
    assert is_us_location("United States") is True
    assert is_us_location("Remote, USA") is True
    assert is_us_location("Remote (U.S.)") is True


def test_city_state_passes():
    assert is_us_location("New York, New York") is True
    assert is_us_location("Palo Alto, California") is True
    assert is_us_location("Dallas, TX") is True
    assert is_us_location("Auburn Hills, Oakland County") is True  # "Michigan" absent but no non-US signal either


def test_bare_us_state_name_passes():
    assert is_us_location("California") is True


def test_foreign_country_fails():
    assert is_us_location("London, United Kingdom") is False
    assert is_us_location("Barcelona, Spain") is False
    assert is_us_location("Hyderabad, India") is False
    assert is_us_location("Toronto, Canada") is False
    assert is_us_location("Mexico City (CDMX), Mexico") is False


def test_region_codes():
    assert is_us_location("APAC") is False
    assert is_us_location("EMEA") is False
    assert is_us_location("NAMER") is True  # ambiguous North America code -- fail-open


def test_semicolon_list_passes_if_any_segment_is_us():
    assert is_us_location("Berlin, Germany; New York, New York") is True


def test_semicolon_list_fails_if_no_segment_is_us():
    location = (
        "Guadalajara, Jalisco, Mexico; Mexico City (CDMX), Mexico; "
        "Monterrey, Nuevo Leon, Mexico; San Jose, Costa Rica"
    )
    assert is_us_location(location) is False


def test_ambiguous_bare_string_passes_permissively():
    assert is_us_location("HQ") is True
    assert is_us_location("Berlin Office") is False  # "berlin" is a recognized non-US signal


def test_bare_european_capitals_without_country_name_fail():
    # Real bug: "Dublin" and "Paris" alone (no "Ireland"/"France" attached)
    # passed permissively before these were added -- reported live via two
    # Stripe postings on Beacon that should have been filtered.
    assert is_us_location("Dublin") is False
    assert is_us_location("Paris") is False
    assert is_us_location("Bengaluru") is False


def test_country_code_prefix_pattern_fails():
    # Real bug: global companies (seen live: Stripe) frequently encode office
    # location as an ISO-3166 alpha-2 country-code prefix, e.g. "AE-Dubai",
    # "IN-Bengaluru" -- none of these had ever been recognized as non-US
    # since no individual city name matched.
    for loc in [
        "AE-Dubai", "AU-Melbourne", "AU-Perth-WW", "BR-Sao Paulo-WW",
        "CH-Zurich-MSO", "CN-Beijing-MSO", "IN-Bengaluru", "IN-Mumbai-MSO",
        "IN-Pune", "JP-Tokyo", "NO-Oslo-MSO", "PL-Warsaw", "SA-Riyadh-MSO",
        "FR-Paris",
    ]:
        assert is_us_location(loc) is False, loc


def test_ambiguous_city_names_use_compound_signals_not_bare_names():
    # "Manchester" (NH/CT), "Melbourne" (FL), and "Geneva" (NY/IL) are all
    # real US places -- a bare city-name signal would wrongly exclude them,
    # so only the specific compound forms seen in real postings are treated
    # as non-US, and the bare US namesakes must keep passing.
    assert is_us_location("Manchester, UK") is False
    assert is_us_location("Manchester, NH") is True
    assert is_us_location("AU-Melbourne") is False
    assert is_us_location("Melbourne, FL") is True
    assert is_us_location("Geneva, GE") is False
    assert is_us_location("Geneva, NY") is True


def test_more_missing_non_us_cities_now_recognized():
    for loc in [
        "Chennai", "Mumbai, IND", "Gurgaon", "Gurugram", "Jaipur", "Tokyo",
        "Jakarta", "Shanghai", "Zurich", "Warsaw", "Luxembourg", "Tallinn",
        "Sao Paulo", "Bucharest", "Phnom Penh, Cambodia", "Escazu, CRI",
        "Uruguay", "Doha, Qatar", "Dubai", "Split, HRV", "Zagreb, HRV",
    ]:
        assert is_us_location(loc) is False, loc


def test_herzliya_does_not_collide_with_illinois_abbreviation():
    # "IL" is both Illinois' state abbreviation and a common shorthand for
    # Israel in job postings -- "Herzliya, IL" must resolve as Israel (a
    # specific Israeli city), not fall through to the state-abbreviation
    # check and get misread as Illinois.
    assert is_us_location("Herzliya, IL") is False


def test_risky_city_names_prefer_a_confirmed_us_match_over_the_foreign_city():
    # Real bug: "South Paris, Oxford County" (a real Maine town) was
    # misclassified as Paris, France, because "paris" was an unconditional
    # non-US signal checked before any US signal. Real US towns exist for
    # several other risky names too (Dublin OH/GA, London OH, Berlin NH,
    # Warsaw IN, Madrid NY, Lisbon OH) -- a "County"/"Parish" suffix or a
    # valid state abbreviation/name must win over the foreign-city guess.
    assert is_us_location("South Paris, Oxford County") is True
    assert is_us_location("Dublin, OH") is True
    assert is_us_location("Dublin, GA") is True
    assert is_us_location("London, OH") is True
    assert is_us_location("Berlin, NH") is True
    assert is_us_location("Warsaw, IN") is True
    assert is_us_location("Madrid, NY") is True
    assert is_us_location("Lisbon, OH") is True


def test_risky_city_names_still_resolve_non_us_without_a_us_signal():
    # The other side of the same fix: with nothing to confirm a US match,
    # these must still resolve as the foreign city (this is what a real
    # Stripe posting's bare "Paris"/"Dublin" location field looks like).
    assert is_us_location("Paris") is False
    assert is_us_location("Dublin") is False
    assert is_us_location("London, United Kingdom") is False
    assert is_us_location("Berlin, Germany") is False
    assert is_us_location("Warsaw") is False
    assert is_us_location("Madrid, Spain") is False
    assert is_us_location("Lisbon") is False


def test_county_or_parish_suffix_confirms_us_before_a_risky_city_check():
    assert is_us_location("Baton Rouge, East Baton Rouge Parish") is True
    assert is_us_location("Mandeville, Saint Tammany Parish") is True
