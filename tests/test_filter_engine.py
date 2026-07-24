from app.filter_engine import evaluate_job, infer_remote_type, infer_seniority, run_filter_engine
from app.job_log import JOB_LOG_COLUMNS
from app.sheets import MAIN_SHEET_COLUMNS
from tests.fakes import FakeWorksheet

DEFAULT_KEYWORDS = {
    "role_keyword_include": ["Solutions Architect"],
    "tech_keyword_include": ["Kafka"],
    "title_exclude": ["Intern"],
    "seniority": ["mid", "senior", "staff"],
    "remote_type": ["remote", "hybrid"],
    "location_include": [],
    "industries_include": [],
}
DEFAULT_SETTINGS = {
    "posted_within_days": 30,
    "company_priority_min": "B",
    "employee_count_min": 0,
    "employee_count_max": None,
    "founded_after_year": None,
    "require_h1b_track_record": False,
    "require_us_location": False,
}


def _job(conn, **overrides):
    fields = {
        "company_id": None,
        "title": "Solutions Architect",
        "url": "https://example.com/1",
        "description": "",
        "location": None,
        "seniority": None,
        "remote_type": None,
        "posted_at": None,
    }
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    cursor = conn.execute(
        f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", tuple(fields.values())
    )
    conn.commit()
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)).fetchone()


def _company(conn, **overrides):
    fields = {
        "name": "Acme",
        "priority_tier": "A",
        "employee_count": 500,
        "founded_year": 2010,
        "industry": None,
        "h1b_sponsor_last_5yrs": None,
    }
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    cursor = conn.execute(
        f"INSERT INTO companies ({columns}) VALUES ({placeholders})", tuple(fields.values())
    )
    conn.commit()
    return conn.execute("SELECT * FROM companies WHERE id = ?", (cursor.lastrowid,)).fetchone()


def test_infer_seniority():
    assert infer_seniority("Senior Solutions Architect") == "senior"
    assert infer_seniority("Staff Solutions Architect") == "staff"
    assert infer_seniority("Solutions Architect Intern") == "junior"
    assert infer_seniority("Solutions Architect") == "mid"


def test_infer_remote_type():
    assert infer_remote_type("Remote - US") == "remote"
    assert infer_remote_type("Hybrid - NYC") == "hybrid"
    assert infer_remote_type("New York, NY") is None
    assert infer_remote_type(None) is None


def test_keyword_mismatch_fails(db_conn):
    job = _job(db_conn, title="Marketing Manager", description="")
    result = evaluate_job(job, None, DEFAULT_SETTINGS, DEFAULT_KEYWORDS)
    assert not result.passed
    assert result.reason == "Filtered Out - Title/Keyword Mismatch"


def test_short_tech_keyword_does_not_match_inside_an_unrelated_word(db_conn):
    # Real false positive: an ICU nursing posting passed the filter because
    # tech_keyword_include's "X12" (EDI standard) substring-matched inside
    # "Weekx12" -- Adzuna's scraped text for "13 Week" + "x12" shift-count
    # concatenated with no space, unrelated to EDI entirely.
    keywords = dict(DEFAULT_KEYWORDS, tech_keyword_include=["X12"], role_keyword_include=[])
    job = _job(
        db_conn, title="RN- Registered Nurse - ICU- Intensive Care Unit",
        description="Duration : 13 Week Shift : Unknown Shifts Per Weekx12 Job Description",
    )
    result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
    assert not result.passed
    assert result.reason == "Filtered Out - Title/Keyword Mismatch"


def test_short_tech_keyword_does_not_match_shift_notation_with_a_leading_space(db_conn):
    # Real false positive, second variant: the leading-boundary fix above
    # doesn't catch this one, since there IS a genuine word boundary before
    # "x12" here (a space) -- "1 to 2 x12-Hour Shifts/Week" (real SSM Health
    # posting text). Requires excluding a match immediately followed by
    # "-hour"/" hour" too.
    keywords = dict(DEFAULT_KEYWORDS, tech_keyword_include=["X12"], role_keyword_include=[])
    job = _job(
        db_conn, title="Respiratory Care Practitioner (RRT)",
        description="Nights, 7p to 7a, 1 to 2 x12-Hour Shifts/Week Shift Differential",
    )
    result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
    assert not result.passed
    assert result.reason == "Filtered Out - Title/Keyword Mismatch"

    # Genuine EDI matches must still work
    job2 = _job(
        db_conn, url="https://example.com/edi-x12",
        title="Software Engineer - Doc Management & EDI X12 Translation",
    )
    result2 = evaluate_job(job2, None, DEFAULT_SETTINGS, keywords)
    assert result2.passed


def test_kong_keyword_does_not_match_hong_kong_the_place_name(db_conn):
    # Real false positive, confirmed live: a restaurant's "Executive Chef"
    # posting (and 12 other completely unrelated jobs -- an MBA leadership
    # program, a bilingual wholesale rep, etc.) passed the filter because
    # tech_keyword_include's "Kong" (the tracked API-gateway company)
    # substring-matched the place name "Hong Kong" in the description
    # ("...Hong Kong-inspired restaurant..."), with no relation to the Kong
    # company at all.
    keywords = dict(DEFAULT_KEYWORDS, tech_keyword_include=["Kong"], role_keyword_include=[])
    job = _job(
        db_conn, title="Executive Chef | Cantonese Cuisine",
        description="A well-respected, Hong Kong-inspired restaurant in Washington, DC is seeking an experienced Executive Chef.",
    )
    result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
    assert not result.passed
    assert result.reason == "Filtered Out - Title/Keyword Mismatch"

    # A genuine Kong (the company) mention must still match
    job2 = _job(
        db_conn, url="https://example.com/kong-role",
        title="Solutions Engineer", description="Experience with Kong API Gateway required.",
    )
    result2 = evaluate_job(job2, None, DEFAULT_SETTINGS, keywords)
    assert result2.passed


def test_company_own_name_excluded_from_its_own_keyword_matches(db_conn):
    # Real false positive: Kong (an API-Gateway vendor) and Boomi (an iPaaS
    # vendor) are both tracked companies whose own name is ALSO a
    # tech_keyword_include entry -- every posting from that company mentions
    # its own name in ordinary boilerplate ("...loyalty to Kong"), so the
    # keyword trivially "matched" regardless of the actual role (Renewal
    # Account Representative, Sales Development Representative, etc).
    kong = _company(db_conn, name="Kong")
    keywords = dict(DEFAULT_KEYWORDS, role_keyword_include=[], tech_keyword_include=["Kong"])
    job = _job(
        db_conn, company_id=kong["id"], title="Renewal Account Representative",
        description="Drive satisfaction and loyalty to Kong. Conduct account reviews.",
    )
    result = evaluate_job(job, kong, DEFAULT_SETTINGS, keywords)
    assert not result.passed
    assert result.reason == "Filtered Out - Title/Keyword Mismatch"

    # The same keyword must still count for a DIFFERENT company's posting
    # that genuinely requires Kong API Gateway as a skill.
    acme = _company(db_conn, name="Acme Corp")
    other_job = _job(
        db_conn, url="https://example.com/kong-skill", company_id=acme["id"],
        title="Platform Engineer", description="Experience with Kong API Gateway required.",
    )
    other_result = evaluate_job(other_job, acme, DEFAULT_SETTINGS, keywords)
    assert other_result.passed


def test_short_tech_keyword_still_matches_as_a_standalone_word(db_conn):
    keywords = dict(DEFAULT_KEYWORDS, tech_keyword_include=["X12"], role_keyword_include=[])
    job = _job(db_conn, title="Solutions Architect", description="Experience with EDI X12 transactions required")
    result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
    assert result.passed


def test_role_keyword_still_matches_its_suffix_form(db_conn):
    # The word-boundary fix only anchors the *start* of the match -- a role
    # keyword must still match its common suffix/plural derivatives, since
    # these are genuinely relevant (not accidental substring collisions like
    # X12/Weekx12): "Architect" -> "Architecture", "Engineer" -> "Engineering",
    # "Integration" -> "Integrations".
    keywords = dict(DEFAULT_KEYWORDS, role_keyword_include=["Enterprise Architect"], tech_keyword_include=[])
    job = _job(
        db_conn, url="https://example.com/suffix-1", title="Solution Architect",
        description="As part of our Enterprise Architecture practice",
    )
    result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
    assert result.passed

    keywords = dict(DEFAULT_KEYWORDS, role_keyword_include=["Customer Engineer"], tech_keyword_include=[])
    job = _job(db_conn, url="https://example.com/suffix-2", title="Director, Customer Engineering", description="")
    result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
    assert result.passed

    keywords = dict(DEFAULT_KEYWORDS, role_keyword_include=[], tech_keyword_include=["Integration"])
    job = _job(
        db_conn, url="https://example.com/suffix-3", title="Software Developer",
        description="data platform integrations using Java",
    )
    result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
    assert result.passed


def test_tech_keyword_in_description_matches(db_conn):
    job = _job(db_conn, title="Platform Engineer", description="Experience with Kafka required")
    result = evaluate_job(job, None, DEFAULT_SETTINGS, DEFAULT_KEYWORDS)
    assert result.passed


def test_title_exclude_fails_even_with_role_match(db_conn):
    job = _job(db_conn, title="Solutions Architect Intern")
    result = evaluate_job(job, None, DEFAULT_SETTINGS, DEFAULT_KEYWORDS)
    assert not result.passed
    assert result.reason == "Filtered Out - Title/Keyword Mismatch"


def test_title_exclude_filters_out_hvac_despite_generic_role_title(db_conn):
    # "Technical Sales Engineer" is a genuine role_keyword_include match --
    # the title really does contain that phrase -- but the role is
    # industry-agnostic (HVAC equipment sales reps get the same generic
    # title as tech presales reps). title_exclude is the right lever here,
    # not a keyword-matching bug.
    keywords = dict(
        DEFAULT_KEYWORDS,
        role_keyword_include=["Technical Sales Engineer"],
        title_exclude=["Intern", "HVAC"],
    )
    job = _job(db_conn, title="Technical Sales Engineer - Custom HVAC Equipment - TX")
    result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
    assert not result.passed
    assert result.reason == "Filtered Out - Title/Keyword Mismatch"


def test_title_exclude_filters_out_industrial_sales_engineer(db_conn):
    # Same class as HVAC -- "Sales Engineer" genuinely matches, but power
    # generation equipment sales is a different industry entirely.
    keywords = dict(
        DEFAULT_KEYWORDS,
        role_keyword_include=["Sales Engineer"],
        title_exclude=["Intern", "Industrial Sales Engineer"],
    )
    job = _job(db_conn, title="Industrial Sales Engineer (Power Generation Equipment)")
    result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
    assert not result.passed
    assert result.reason == "Filtered Out - Title/Keyword Mismatch"


def test_title_exclude_filters_out_therapist_and_clinical_supervisor(db_conn):
    # Real ambiguity: "MFT" in tech_keyword_include means "Managed File
    # Transfer", but the mental-health field uses the same acronym for
    # "Marriage and Family Therapist" -- both are genuine standalone uses,
    # so removing the keyword would cost real IT/MFT matches elsewhere.
    # title_exclude targets the specific offending titles instead.
    keywords = dict(
        DEFAULT_KEYWORDS,
        role_keyword_include=[], tech_keyword_include=["MFT"],
        title_exclude=["Intern", "Therapist", "Clinical Supervisor"],
    )
    for title in ["Therapist - Unlicensed (Msw, Mft, Mhc)", "Clinical Supervisor MSW-MFT"]:
        job = _job(db_conn, url=f"https://example.com/{title}", title=title)
        result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
        assert not result.passed
        assert result.reason == "Filtered Out - Title/Keyword Mismatch"


def test_title_exclude_filters_out_office_assistant_despite_tech_boilerplate(db_conn):
    # Real case: an "Office Assistant" posting at Workato passed the tech
    # keyword check because Workato's boilerplate "About Us" text in the
    # description mentions "iPaaS" -- present in every posting from that
    # company regardless of the actual role.
    keywords = dict(
        DEFAULT_KEYWORDS,
        tech_keyword_include=["iPaaS"],
        title_exclude=["Intern", "Office Assistant"],
    )
    job = _job(
        db_conn, title="Office Assistant",
        description="Workato delivers enterprise infrastructure for the agentic era, redefining iPaaS...",
    )
    result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
    assert not result.passed
    assert result.reason == "Filtered Out - Title/Keyword Mismatch"


def test_title_exclude_covers_remaining_mft_collision_titles_but_preserves_real_it_matches(db_conn):
    # A batch of real mental-health titles that all matched only via the same
    # "MFT" collision, plus the two genuine Managed-File-Transfer IT
    # postings that must keep passing.
    keywords = dict(
        DEFAULT_KEYWORDS,
        role_keyword_include=[], tech_keyword_include=["MFT"],
        title_exclude=["Intern", "Clinician", "LMFT", "Parent Partner", "Assessment Counselor", "Registered Nurse"],
    )
    bad_titles = [
        "Clinician (Lead) MFT, LCSW, LPCC Program Supervisor",
        "LMFT/MFT LP",
        "MFT Parent Partner",
        "Behavioral Health Clinician (CSWA, MFT-A, LPC-A) - Hybrid",
        "Assessment Counselor (LSW, LPC, MFT)",
        "Registered Nurse - Oncology - Days - MFT",
    ]
    for title in bad_titles:
        job = _job(db_conn, url=f"https://example.com/{title}", title=title)
        result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
        assert not result.passed, f"{title!r} should have been filtered out"
        assert result.reason == "Filtered Out - Title/Keyword Mismatch"

    for title in ["Systems Engineer - MFT", "Production Support Analyst"]:
        job = _job(
            db_conn, url=f"https://example.com/{title}", title=title,
            description="IBM middleware technologies, MFT, MQ, Datapower" if "Production" in title else "",
        )
        result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
        assert result.passed, f"{title!r} should still pass"


def test_title_exclude_filters_out_technical_instructor_despite_tech_boilerplate(db_conn):
    # Same Workato "About Us" boilerplate issue as Office Assistant -- a
    # training/enablement role, not a fit, that only passed via "iPaaS" in
    # company boilerplate text present on every posting.
    keywords = dict(
        DEFAULT_KEYWORDS,
        tech_keyword_include=["iPaaS"],
        title_exclude=["Intern", "Technical Instructor"],
    )
    job = _job(
        db_conn, title="Technical Instructor",
        description="Workato delivers enterprise infrastructure for the agentic era, redefining iPaaS...",
    )
    result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
    assert not result.passed
    assert result.reason == "Filtered Out - Title/Keyword Mismatch"


def test_seniority_mismatch_fails(db_conn):
    job = _job(db_conn, title="Solutions Architect", seniority="junior")
    result = evaluate_job(job, None, DEFAULT_SETTINGS, DEFAULT_KEYWORDS)
    assert not result.passed
    assert result.reason == "Filtered Out - Seniority"


def test_remote_type_mismatch_fails(db_conn):
    job = _job(db_conn, title="Solutions Architect", remote_type="onsite")
    result = evaluate_job(job, None, DEFAULT_SETTINGS, DEFAULT_KEYWORDS)
    assert not result.passed
    assert result.reason == "Filtered Out - Location"


def test_location_include_mismatch_fails(db_conn):
    # Matches the resolved location_state, not raw location text -- a job
    # actually in CA against a TX-only filter must fail even though nothing
    # in the raw location string itself mentions "TX".
    keywords = dict(DEFAULT_KEYWORDS, location_include=["TX"])
    job = _job(db_conn, title="Solutions Architect", location="San Francisco, CA", location_state="CA")
    result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
    assert not result.passed
    assert result.reason == "Filtered Out - Location"


def test_location_include_match_passes(db_conn):
    keywords = dict(DEFAULT_KEYWORDS, location_include=["TX"])
    job = _job(db_conn, title="Solutions Architect", location="Austin, Texas", location_state="TX")
    result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
    assert result.passed


def test_location_include_is_case_insensitive(db_conn):
    keywords = dict(DEFAULT_KEYWORDS, location_include=["tx"])
    job = _job(db_conn, title="Solutions Architect", location="Austin, Texas", location_state="TX")
    result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
    assert result.passed


def test_location_include_permissive_when_location_state_unresolved(db_conn):
    # A missing signal isn't evidence of a mismatch -- same rule this module
    # already applies to remote_type. An informal place name or non-US
    # remote posting that location_state couldn't resolve must not be
    # filtered out just because it's blank.
    keywords = dict(DEFAULT_KEYWORDS, location_include=["TX"])
    job = _job(db_conn, title="Solutions Architect", location="Somewhere Ambiguous", location_state=None)
    result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
    assert result.passed


def test_require_us_location_filters_foreign_jobs(db_conn):
    settings = dict(DEFAULT_SETTINGS, require_us_location=True)
    job = _job(db_conn, title="Solutions Architect", location="London, United Kingdom")
    result = evaluate_job(job, None, settings, DEFAULT_KEYWORDS)
    assert not result.passed
    assert result.reason == "Filtered Out - Location"
    assert result.skip_log is True  # non-US jobs are high-volume noise -- not worth a Job Log row


def test_other_location_filters_still_log(db_conn):
    # skip_log is specific to require_us_location -- remote_type/location_include
    # mismatches still get logged normally.
    keywords = dict(DEFAULT_KEYWORDS, location_include=["TX"])
    job = _job(db_conn, title="Solutions Architect", location="San Francisco, CA", location_state="CA")
    result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
    assert not result.passed
    assert result.skip_log is False


def test_require_us_location_passes_us_jobs(db_conn):
    settings = dict(DEFAULT_SETTINGS, require_us_location=True)
    job = _job(db_conn, title="Solutions Architect", location="Austin, Texas")
    result = evaluate_job(job, None, settings, DEFAULT_KEYWORDS)
    assert result.passed


def test_posted_too_long_ago_fails(db_conn):
    job = _job(db_conn, title="Solutions Architect", posted_at="2020-01-01T00:00:00Z")
    result = evaluate_job(job, None, DEFAULT_SETTINGS, DEFAULT_KEYWORDS)
    assert not result.passed
    assert result.reason == "Filtered Out - Posted Date"


def test_company_priority_below_min_fails(db_conn):
    job = _job(db_conn, title="Solutions Architect")
    company = _company(db_conn, priority_tier="C")
    result = evaluate_job(job, company, DEFAULT_SETTINGS, DEFAULT_KEYWORDS)
    assert not result.passed
    assert result.reason == "Filtered Out - Company Criteria"


def test_employee_count_below_min_fails(db_conn):
    settings = dict(DEFAULT_SETTINGS, employee_count_min=1000)
    job = _job(db_conn, title="Solutions Architect")
    company = _company(db_conn, employee_count=50)
    result = evaluate_job(job, company, settings, DEFAULT_KEYWORDS)
    assert not result.passed
    assert result.reason == "Filtered Out - Company Criteria"


def test_require_h1b_track_record_fails_when_unknown(db_conn):
    settings = dict(DEFAULT_SETTINGS, require_h1b_track_record=True)
    job = _job(db_conn, title="Solutions Architect")
    company = _company(db_conn, h1b_sponsor_last_5yrs=None)
    result = evaluate_job(job, company, settings, DEFAULT_KEYWORDS)
    assert not result.passed
    assert result.reason == "Filtered Out - Company Criteria"


def test_unknown_company_attributes_pass_permissively(db_conn):
    job = _job(db_conn, title="Solutions Architect")
    company = _company(
        db_conn, priority_tier=None, employee_count=None, founded_year=None, industry=None
    )
    result = evaluate_job(job, company, DEFAULT_SETTINGS, DEFAULT_KEYWORDS)
    assert result.passed


def test_fully_passing_job(db_conn):
    job = _job(db_conn, title="Senior Solutions Architect", location="Remote - US", remote_type="remote")
    company = _company(db_conn, priority_tier="S")
    result = evaluate_job(job, company, DEFAULT_SETTINGS, DEFAULT_KEYWORDS)
    assert result.passed


def test_singular_solution_architect_matches_role_keyword(db_conn):
    # Real gap: "Solution Architect" (singular) is a very common alternate
    # spelling that wasn't in role_keyword_include (only the plural
    # "Solutions Architect" was) -- genuinely relevant postings (Amazon
    # "Senior Solution Architect, Healthcare", TD Synnex "Presales Solution
    # Architect") were only slipping through by accident via an unrelated
    # generic tech keyword match.
    keywords = dict(DEFAULT_KEYWORDS, role_keyword_include=["Solution Architect"], tech_keyword_include=[])
    job = _job(db_conn, title="Senior Solution Architect, Healthcare")
    result = evaluate_job(job, None, DEFAULT_SETTINGS, keywords)
    assert result.passed


def test_run_filter_engine_updates_db_and_persists_inferred_fields(db_conn):
    db_conn.execute(
        "INSERT INTO filter_keywords (category, keyword) VALUES ('role_keyword_include', 'Solutions Architect')"
    )
    db_conn.execute(
        "INSERT INTO filter_keywords (category, keyword) VALUES ('title_exclude', 'Intern')"
    )
    db_conn.commit()

    _job(db_conn, title="Senior Solutions Architect", location="Remote - US", url="https://example.com/1")
    _job(db_conn, title="Marketing Manager", url="https://example.com/2")

    result = run_filter_engine(db_conn)
    assert result["evaluated"] == 2
    assert result["passed"] == 1
    assert result["filtered_out"] == 1

    passing = db_conn.execute(
        "SELECT status, seniority, remote_type FROM jobs WHERE title = 'Senior Solutions Architect'"
    ).fetchone()
    assert passing["status"] == "new"
    assert passing["seniority"] == "senior"
    assert passing["remote_type"] == "remote"

    failing = db_conn.execute(
        "SELECT status, rejection_reason FROM jobs WHERE title = 'Marketing Manager'"
    ).fetchone()
    assert failing["status"] == "filtered_out"
    assert failing["rejection_reason"] == "Filtered Out - Title/Keyword Mismatch"


def test_run_filter_engine_ignores_non_new_jobs(db_conn):
    db_conn.execute(
        "INSERT INTO filter_keywords (category, keyword) VALUES ('role_keyword_include', 'Solutions Architect')"
    )
    db_conn.commit()
    job = _job(db_conn, title="Solutions Architect")
    db_conn.execute("UPDATE jobs SET status = 'notified' WHERE id = ?", (job["id"],))
    db_conn.commit()

    result = run_filter_engine(db_conn)
    assert result["evaluated"] == 0


def test_run_filter_engine_writes_job_log_on_filter_out(db_conn):
    db_conn.execute(
        "INSERT INTO filter_keywords (category, keyword) VALUES ('role_keyword_include', 'Solutions Architect')"
    )
    db_conn.commit()
    _job(db_conn, title="Marketing Manager")

    job_log_ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS])
    run_filter_engine(db_conn, job_log_ws=job_log_ws)

    assert len(job_log_ws.appended) == 1
    row = dict(zip(JOB_LOG_COLUMNS, job_log_ws.appended[0]))
    assert row["Reason for Rejection"] == (
        "Filtered Out - Title/Keyword Mismatch: no role/tech keyword matched title or description"
    )


def test_run_filter_engine_adds_passing_job_to_beacon_immediately(db_conn):
    db_conn.execute(
        "INSERT INTO filter_keywords (category, keyword) VALUES ('role_keyword_include', 'Solutions Architect')"
    )
    db_conn.commit()
    _job(db_conn, title="Solutions Architect")

    main_ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS])
    run_filter_engine(db_conn, main_ws=main_ws)

    assert len(main_ws.appended) == 1
    row = dict(zip(MAIN_SHEET_COLUMNS, main_ws.appended[0]))
    assert row["Title"] == "Solutions Architect"
    assert row["Initial Fit Score"] == ""  # no waiting on a score


def test_run_filter_engine_no_job_log_write_on_pass(db_conn):
    db_conn.execute(
        "INSERT INTO filter_keywords (category, keyword) VALUES ('role_keyword_include', 'Solutions Architect')"
    )
    db_conn.commit()
    _job(db_conn, title="Solutions Architect")

    job_log_ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS])
    run_filter_engine(db_conn, job_log_ws=job_log_ws)

    # passing jobs go straight to Beacon now, not the Job Log
    assert job_log_ws.appended == []


def test_run_filter_engine_no_job_log_write_for_non_us_location(db_conn):
    db_conn.execute(
        "INSERT INTO filter_keywords (category, keyword) VALUES ('role_keyword_include', 'Solutions Architect')"
    )
    db_conn.commit()
    job = _job(db_conn, title="Solutions Architect", location="London, United Kingdom")

    job_log_ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS])
    # require_us_location is read from filter_settings, not passed directly --
    # set it via the same table run_filter_engine reads from.
    db_conn.execute(
        "INSERT INTO filter_settings (key, value) VALUES ('require_us_location', 'true')"
    )
    db_conn.commit()

    result = run_filter_engine(db_conn, job_log_ws=job_log_ws)

    assert result["filtered_out"] == 1
    assert job_log_ws.appended == []  # silently dropped, no Job Log noise

    row = db_conn.execute("SELECT status, rejection_reason FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["status"] == "filtered_out"
    assert row["rejection_reason"] == "Filtered Out - Location"
