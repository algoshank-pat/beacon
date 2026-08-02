import pytest

from app.job_log import JOB_LOG_COLUMNS
from app.sheets import (
    MAIN_SHEET_COLUMNS,
    JOB_ID_COL_INDEX,
    VISA_SCORE_REQUEST_BUDGET_REACHED,
    VISA_SCORE_REQUEST_COL_INDEX,
    VISA_SCORE_REQUEST_PROCESSING,
    VISA_SCORE_REQUEST_REQUEST,
)
from app.visa_scan import (
    VISA_FLAG_NEEDS_REVIEW,
    VISA_FLAG_NO_MENTION,
    VISA_FLAG_PENDING,
    gemini_classify,
    haiku_classify,
    mentions_sponsorship_keywords,
    regex_classify,
    run_visa_scan,
)
from tests.fakes import FakeGeminiClient, FakeWorksheet


def _requested_worksheet(*job_ids: int) -> FakeWorksheet:
    """A Beacon worksheet with one row per job_id, each already marked
    "Request Check" -- the explicit per-job gate Pass 2 (Haiku) requires
    before it will spend anything on that job."""
    rows = [MAIN_SHEET_COLUMNS]
    for job_id in job_ids:
        row = [""] * len(MAIN_SHEET_COLUMNS)
        row[JOB_ID_COL_INDEX - 1] = str(job_id)
        row[VISA_SCORE_REQUEST_COL_INDEX - 1] = VISA_SCORE_REQUEST_REQUEST
        rows.append(row)
    return FakeWorksheet(rows=rows)


@pytest.fixture(autouse=True)
def _no_page_fetch_by_default(monkeypatch):
    # run_visa_scan now fetches the full posting page for every job with a
    # URL before classification (see app.visa_scan's module docstring) --
    # default every test to "fetch unavailable" so they exercise the
    # existing fallback-to-stored-description path and none of them makes a
    # real HTTP call. Tests that specifically want to verify the full-page
    # path override this with their own monkeypatch.
    monkeypatch.setattr("app.visa_scan.fetch_job_page_text", lambda url, session=None: None)


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeResponse:
    def __init__(self, text, input_tokens=100, output_tokens=20):
        self.content = [_FakeTextBlock(text)]
        self.usage = _FakeUsage(input_tokens, output_tokens)


class _FakeMessages:
    def __init__(self, response_text):
        self._response_text = response_text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._response_text)


class _FakeClient:
    def __init__(self, response_text='{"visa_flag": "unclear", "snippet": ""}'):
        self.messages = _FakeMessages(response_text)


def test_regex_classify_detects_restriction():
    flag, snippet = regex_classify("We are unable to sponsor visas at this time.")
    assert flag == "restricted"
    assert "sponsor" in snippet.lower()


def test_regex_classify_detects_sponsorship_not_available():
    flag, snippet = regex_classify(
        "Candidates must have current work authorization in the US, "
        "Visa sponsorship is not available for this position"
    )
    assert flag == "restricted"
    assert "sponsorship" in snippet.lower()


def test_regex_classify_detects_does_not_now_or_future_require_sponsorship():
    flag, snippet = regex_classify(
        "Applicants must have a valid work authorization that does not now, or in "
        "the future, require visa sponsorship for employment in the United States (eg., H1B)."
    )
    assert flag == "restricted"
    assert "sponsorship" in snippet.lower()


def test_regex_classify_detects_not_open_to_sponsorship():
    flag, snippet = regex_classify(
        "VISA SPONSORSHIP: If an applicant currently holds a VISA, they are not "
        "eligible for this role. This role is not open to VISA Sponsorship."
    )
    assert flag == "restricted"
    assert "sponsorship" in snippet.lower()


def test_regex_classify_detects_without_visa_transfer_or_sponsorship():
    flag, snippet = regex_classify(
        "Must have legal authorization to work permanently in the United States "
        "for any employer without requiring a visa transfer or visa sponsorship"
    )
    assert flag == "restricted"
    assert "sponsorship" in snippet.lower()


def test_regex_classify_detects_sponsor_friendly():
    flag, snippet = regex_classify("Visa sponsorship available for qualified candidates.")
    assert flag == "sponsors"


def test_regex_classify_ambiguous_returns_none():
    flag, snippet = regex_classify("This role focuses on integration architecture.")
    assert flag is None
    assert snippet is None


def test_haiku_classify_parses_structured_response():
    client = _FakeClient('{"visa_flag": "restricted", "snippet": "must have work authorization"}')
    result, usage = haiku_classify(client, "some ambiguous JD text")
    assert result["visa_flag"] == "restricted"
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 20


def test_gemini_classify_parses_structured_response():
    client = FakeGeminiClient('{"visa_flag": "restricted", "snippet": "must have work authorization"}')
    result, usage = gemini_classify(client, "some ambiguous JD text")
    assert result["visa_flag"] == "restricted"
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 50


def test_run_visa_scan_dispatches_to_gemini_when_configured(db_conn):
    job_id = db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA', 'https://x/1', 'Ambiguous mention of visa policy, no clear regex match.', 'new')"
    ).lastrowid
    db_conn.commit()

    client = FakeGeminiClient('{"visa_flag": "unclear", "snippet": "visa policy"}')
    main_ws = _requested_worksheet(job_id)
    result = run_visa_scan(
        db_conn, client, {"require_visa_sponsorship": False}, provider="gemini", main_ws=main_ws,
    )

    assert result["haiku_calls"] == 1
    assert len(client.models.calls) == 1
    assert client.models.calls[0]["model"] == "gemini-3.5-flash-lite"
    row = db_conn.execute("SELECT visa_flag FROM jobs").fetchone()
    assert row["visa_flag"] == "unclear"


def test_run_visa_scan_uses_regex_when_confident(db_conn):
    db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA', 'https://x/1', 'We will sponsor a work visa for this role.', 'new')"
    )
    db_conn.commit()

    client = _FakeClient()
    result = run_visa_scan(db_conn, client, {"require_visa_sponsorship": False})

    assert result["regex_hits"] == 1
    assert result["haiku_calls"] == 0
    row = db_conn.execute("SELECT visa_flag FROM jobs").fetchone()
    assert row["visa_flag"] == "sponsors"


def test_run_visa_scan_classifies_using_the_full_fetched_page_not_the_truncated_description(db_conn, monkeypatch):
    # The core fix: a truncated (e.g. Adzuna 500-char) description with no
    # sponsorship language at all, but the full posting page has a
    # restriction disclaimer near the end -- classification must use the
    # fetched full text, not the short stored description.
    db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA', 'https://x/1', 'Build integrations using Kafka and MuleSoft.', 'new')"
    )
    db_conn.commit()

    full_page = (
        "Build integrations using Kafka and MuleSoft. " + "Filler text. " * 50
        + "At this time, we typically do not offer visa sponsorship for this position."
    )
    monkeypatch.setattr("app.visa_scan.fetch_job_page_text", lambda url, session=None: full_page)

    client = _FakeClient()
    result = run_visa_scan(db_conn, client, {"require_visa_sponsorship": False})

    assert result["no_mention"] == 0
    row = db_conn.execute("SELECT visa_flag, visa_snippet FROM jobs").fetchone()
    assert row["visa_flag"] == "restricted"
    assert "visa sponsorship" in row["visa_snippet"]


def test_run_visa_scan_falls_back_to_stored_description_when_page_fetch_fails(db_conn, monkeypatch):
    db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA', 'https://x/1', 'We will sponsor a work visa for this role.', 'new')"
    )
    db_conn.commit()

    def _boom(url, session=None):
        raise Exception("connection refused")

    monkeypatch.setattr("app.visa_scan.fetch_job_page_text", _boom)

    client = _FakeClient()
    result = run_visa_scan(db_conn, client, {"require_visa_sponsorship": False})

    assert result["regex_hits"] == 1
    row = db_conn.execute("SELECT visa_flag FROM jobs").fetchone()
    assert row["visa_flag"] == "sponsors"




def test_mentions_sponsorship_keywords_true_when_present():
    assert mentions_sponsorship_keywords("Please note visa status will be discussed.") is True
    assert mentions_sponsorship_keywords("We support H1B transfers.") is True
    assert mentions_sponsorship_keywords("Must show proof of work permit.") is True


def test_mentions_sponsorship_keywords_false_when_absent():
    assert mentions_sponsorship_keywords("Build integrations using Kafka and MuleSoft.") is False
    assert mentions_sponsorship_keywords("") is False


def test_run_visa_scan_classifies_no_mention_for_free_when_no_keywords_present(db_conn):
    db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA', 'https://x/1', 'Build integrations using Kafka and MuleSoft.', 'new')"
    )
    db_conn.commit()

    client = _FakeClient()
    result = run_visa_scan(db_conn, client, {"require_visa_sponsorship": False})

    assert result["regex_hits"] == 0
    assert result["no_mention"] == 1
    assert result["haiku_calls"] == 0
    assert client.messages.calls == []  # no Haiku call made at all
    row = db_conn.execute("SELECT visa_flag FROM jobs").fetchone()
    assert row["visa_flag"] == VISA_FLAG_NO_MENTION


def test_run_visa_scan_needs_review_for_free_when_keyword_present_but_ambiguous_and_unmarked(db_conn):
    # No main_ws mark at all -- Haiku must never be called just because the
    # free tiers found a job ambiguous. It should land at NEEDS_REVIEW and
    # stop there.
    db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA', 'https://x/1', 'Please note visa status will be discussed during the interview.', 'new')"
    )
    db_conn.commit()

    client = _FakeClient('{"visa_flag": "unclear", "snippet": ""}')
    result = run_visa_scan(db_conn, client, {"require_visa_sponsorship": False})

    assert result["regex_hits"] == 0
    assert result["no_mention"] == 0
    assert result["needs_review"] == 1
    assert result["haiku_calls"] == 0
    assert client.messages.calls == []  # no Haiku call made at all
    row = db_conn.execute("SELECT visa_flag FROM jobs").fetchone()
    assert row["visa_flag"] == VISA_FLAG_NEEDS_REVIEW


def test_run_visa_scan_calls_haiku_once_marked_request_check(db_conn):
    job_id = db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA', 'https://x/1', 'Please note visa status will be discussed during the interview.', 'new')"
    ).lastrowid
    db_conn.commit()

    client = _FakeClient('{"visa_flag": "unclear", "snippet": ""}')
    main_ws = _requested_worksheet(job_id)
    result = run_visa_scan(db_conn, client, {"require_visa_sponsorship": False}, main_ws=main_ws)

    assert result["needs_review"] == 1  # Pass 1 still resolves it to NEEDS_REVIEW first
    assert result["haiku_calls"] == 1  # then Pass 2 picks it up since it was marked
    row = db_conn.execute("SELECT visa_flag FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["visa_flag"] == "unclear"
    row_dict = dict(zip(MAIN_SHEET_COLUMNS, main_ws.rows[1]))
    assert row_dict["Visa Score Request"] == ""  # cleared once fulfilled


def test_run_visa_scan_stops_calling_haiku_once_daily_budget_exhausted(db_conn):
    job_ids = []
    for i in range(2):
        job_ids.append(db_conn.execute(
            "INSERT INTO jobs (title, url, description, status) VALUES (?, ?, "
            "'Please note visa status will be discussed during the interview.', 'new')",
            (f"SA {i}", f"https://x/{i}"),
        ).lastrowid)
    db_conn.commit()

    client = _FakeClient('{"visa_flag": "unclear", "snippet": ""}')
    main_ws = _requested_worksheet(*job_ids)  # both marked -- budget, not the mark, is what blocks job 2
    # Default fake usage is 100 input + 20 output tokens per call -> $0.0002
    # at claude-haiku-4-5's $1.00/$5.00-per-MTok rate (see app.budget
    # PRICING_PER_MTOK). A $0.00015 budget lets the first call through
    # (checked before any spend happens) but blocks the second.
    result = run_visa_scan(
        db_conn, client, {"require_visa_sponsorship": False, "daily_token_budget": 0.00015}, main_ws=main_ws,
    )

    assert result["needs_review"] == 2  # Pass 1 resolves both to NEEDS_REVIEW regardless of budget
    assert result["haiku_calls"] == 1
    assert result["budget_exceeded"] is True
    flags = [row["visa_flag"] for row in db_conn.execute("SELECT visa_flag FROM jobs ORDER BY id")]
    # Job 2 never reached Haiku -- stays NEEDS_REVIEW, retried next run since
    # its "Visa Score Request" mark is untouched by the budget-exceeded path
    # below (only set to "Budget Reached" for the job budget ran out ON, not
    # every job still waiting behind it).
    assert flags == ["unclear", VISA_FLAG_NEEDS_REVIEW]
    row_dict_2 = dict(zip(MAIN_SHEET_COLUMNS, main_ws.rows[2]))
    assert row_dict_2["Visa Score Request"] == VISA_SCORE_REQUEST_BUDGET_REACHED


def test_run_visa_scan_regex_and_no_mention_paths_unaffected_by_exhausted_budget(db_conn):
    db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA', 'https://x/1', 'We are unable to sponsor visas at this time.', 'new')"
    )
    db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA2', 'https://x/2', 'This role focuses on integration architecture.', 'new')"
    )
    db_conn.commit()

    client = _FakeClient()
    result = run_visa_scan(
        db_conn, client, {"require_visa_sponsorship": False, "daily_token_budget": 0.0},
    )

    assert result["regex_hits"] == 1
    assert result["no_mention"] == 1
    assert result["haiku_calls"] == 0
    assert result["budget_exceeded"] is False  # never attempted a Haiku call, so never tripped


def test_run_visa_scan_marks_processing_before_haiku_call_and_retries_on_failure(db_conn):
    job_id = db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA', 'https://x/1', 'Visa sponsorship details available on request.', 'new')"
    ).lastrowid
    db_conn.commit()

    class _FailingClient:
        class _Messages:
            def create(self, **kwargs):
                raise RuntimeError("credit balance is too low")

        def __init__(self):
            self.messages = self._Messages()

    main_ws = _requested_worksheet(job_id)
    result = run_visa_scan(db_conn, _FailingClient(), {"require_visa_sponsorship": False}, main_ws=main_ws)

    assert result["haiku_calls"] == 0
    assert result["haiku_failures"] == 1
    row = db_conn.execute("SELECT visa_flag FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["visa_flag"] == VISA_FLAG_NEEDS_REVIEW  # unchanged -- Haiku never actually returned anything
    row_dict = dict(zip(MAIN_SHEET_COLUMNS, main_ws.rows[1]))
    assert row_dict["Visa Score Request"] == VISA_SCORE_REQUEST_PROCESSING  # stays retryable, not cleared


def test_run_visa_scan_a_failed_call_does_not_abort_the_rest_of_the_batch(db_conn):
    job_id_1 = db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA', 'https://x/1', 'Visa sponsorship details available on request.', 'new')"
    ).lastrowid
    job_id_2 = db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA', 'https://x/2', 'Please note visa status is discussed at interview time.', 'new')"
    ).lastrowid
    db_conn.commit()

    class _FailOnceClient:
        class _Messages:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("credit balance is too low")
                return _FakeResponse('{"visa_flag": "sponsors", "snippet": "discussed at interview"}')

        def __init__(self):
            self.messages = self._Messages()

    main_ws = _requested_worksheet(job_id_1, job_id_2)
    result = run_visa_scan(db_conn, _FailOnceClient(), {"require_visa_sponsorship": False}, main_ws=main_ws)

    assert result["needs_review"] == 2
    assert result["haiku_failures"] == 1
    assert result["haiku_calls"] == 1
    # job 2's Haiku call must still happen even though job 1's failed first
    row2 = db_conn.execute("SELECT visa_flag FROM jobs WHERE id = ?", (job_id_2,)).fetchone()
    assert row2["visa_flag"] == "sponsors"


def test_run_visa_scan_retries_a_job_stuck_processing_from_a_prior_failed_attempt(db_conn):
    # Simulates the retry case: a previous run already resolved this job to
    # NEEDS_REVIEW and attempted Haiku, but that attempt failed, leaving
    # "Visa Score Request" at "Processing" (not cleared, not "Budget
    # Reached"). It must be retried automatically -- the user shouldn't have
    # to re-mark "Request Check" themselves.
    job_id = db_conn.execute(
        "INSERT INTO jobs (title, url, description, status, visa_flag) VALUES "
        "('SA', 'https://x/1', 'Visa sponsorship details available on request.', 'new', ?)",
        (VISA_FLAG_NEEDS_REVIEW,),
    ).lastrowid
    db_conn.commit()

    rows = [MAIN_SHEET_COLUMNS]
    row = [""] * len(MAIN_SHEET_COLUMNS)
    row[JOB_ID_COL_INDEX - 1] = str(job_id)
    row[VISA_SCORE_REQUEST_COL_INDEX - 1] = VISA_SCORE_REQUEST_PROCESSING
    rows.append(row)
    main_ws = FakeWorksheet(rows=rows)

    client = _FakeClient('{"visa_flag": "sponsors", "snippet": "details available"}')
    result = run_visa_scan(db_conn, client, {"require_visa_sponsorship": False}, main_ws=main_ws)

    assert result["scanned"] == 0  # Pass 1 skips it -- visa_flag is not NULL
    assert result["haiku_calls"] == 1  # Pass 2 retries it via the "Processing" mark
    row = db_conn.execute("SELECT visa_flag FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["visa_flag"] == "sponsors"


def test_run_visa_scan_retries_a_job_stuck_at_budget_reached(db_conn):
    # Same retry guarantee, for the other non-cleared request state.
    job_id = db_conn.execute(
        "INSERT INTO jobs (title, url, description, status, visa_flag) VALUES "
        "('SA', 'https://x/1', 'Visa sponsorship details available on request.', 'new', ?)",
        (VISA_FLAG_NEEDS_REVIEW,),
    ).lastrowid
    db_conn.commit()

    rows = [MAIN_SHEET_COLUMNS]
    row = [""] * len(MAIN_SHEET_COLUMNS)
    row[JOB_ID_COL_INDEX - 1] = str(job_id)
    row[VISA_SCORE_REQUEST_COL_INDEX - 1] = VISA_SCORE_REQUEST_BUDGET_REACHED
    rows.append(row)
    main_ws = FakeWorksheet(rows=rows)

    client = _FakeClient('{"visa_flag": "restricted", "snippet": "details available"}')
    result = run_visa_scan(db_conn, client, {"require_visa_sponsorship": False}, main_ws=main_ws)

    assert result["haiku_calls"] == 1
    row = db_conn.execute("SELECT visa_flag FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["visa_flag"] == "restricted"


def test_run_visa_scan_filters_restricted_when_required(db_conn):
    db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA', 'https://x/1', 'We do not sponsor visas for this position.', 'new')"
    )
    db_conn.commit()

    client = _FakeClient()
    result = run_visa_scan(db_conn, client, {"require_visa_sponsorship": True})

    assert result["restricted_filtered"] == 1
    row = db_conn.execute("SELECT status, rejection_reason FROM jobs").fetchone()
    assert row["status"] == "filtered_out"
    assert row["rejection_reason"] == "Visa Restricted"


def test_run_visa_scan_does_not_filter_when_not_required(db_conn):
    db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA', 'https://x/1', 'We do not sponsor visas for this position.', 'new')"
    )
    db_conn.commit()

    client = _FakeClient()
    result = run_visa_scan(db_conn, client, {"require_visa_sponsorship": False})

    assert result["restricted_filtered"] == 0
    row = db_conn.execute("SELECT status FROM jobs").fetchone()
    assert row["status"] == "new"


def test_run_visa_scan_skips_already_scanned_jobs(db_conn):
    db_conn.execute(
        "INSERT INTO jobs (title, url, description, status, visa_flag) VALUES "
        "('SA', 'https://x/1', 'text', 'new', 'sponsors')"
    )
    db_conn.commit()

    client = _FakeClient()
    result = run_visa_scan(db_conn, client, {"require_visa_sponsorship": False})
    assert result["scanned"] == 0


def test_run_visa_scan_respects_limit(db_conn):
    for i in range(5):
        db_conn.execute(
            "INSERT INTO jobs (title, url, description, status) VALUES (?, ?, 'no visa language here', 'new')",
            (f"SA {i}", f"https://x/{i}"),
        )
    db_conn.commit()

    client = _FakeClient()
    result = run_visa_scan(db_conn, client, {"require_visa_sponsorship": False}, limit=2)
    assert result["scanned"] == 2

    remaining = db_conn.execute("SELECT COUNT(*) AS c FROM jobs WHERE visa_flag IS NULL").fetchone()["c"]
    assert remaining == 3


def test_run_visa_scan_writes_job_log_on_restricted_filter(db_conn):
    db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA', 'https://x/1', 'We do not sponsor visas for this position.', 'new')"
    )
    db_conn.commit()

    client = _FakeClient()
    job_log_ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS])
    run_visa_scan(db_conn, client, {"require_visa_sponsorship": True}, job_log_ws=job_log_ws)

    assert len(job_log_ws.appended) == 1
    row = dict(zip(JOB_LOG_COLUMNS, job_log_ws.appended[0]))
    assert row["Reason for Rejection"].startswith("Visa Restricted")


def test_run_visa_scan_no_job_log_write_when_not_restricted(db_conn):
    db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA', 'https://x/1', 'We will sponsor a work visa for this role.', 'new')"
    )
    db_conn.commit()

    client = _FakeClient()
    job_log_ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS])
    run_visa_scan(db_conn, client, {"require_visa_sponsorship": True}, job_log_ws=job_log_ws)

    assert job_log_ws.appended == []


def test_run_visa_scan_updates_beacon_visa_flag_when_not_restricted(db_conn):
    job_id = db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA', 'https://x/1', 'We will sponsor a work visa for this role.', 'new')"
    ).lastrowid
    db_conn.commit()

    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job_id)
    main_ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    client = _FakeClient()
    run_visa_scan(db_conn, client, {"require_visa_sponsorship": True}, main_ws=main_ws)

    row = dict(zip(MAIN_SHEET_COLUMNS, main_ws.rows[1]))
    assert row["Visa Flag"] == "Sponsored"


def test_run_visa_scan_evicts_from_beacon_when_restricted_and_required(db_conn):
    job_id = db_conn.execute(
        "INSERT INTO jobs (title, url, description, status) VALUES "
        "('SA', 'https://x/1', 'We do not sponsor visas for this position.', 'new')"
    ).lastrowid
    db_conn.commit()

    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job_id)
    main_ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])
    job_log_ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS])

    client = _FakeClient()
    run_visa_scan(
        db_conn, client, {"require_visa_sponsorship": True}, main_ws=main_ws, job_log_ws=job_log_ws
    )

    assert len(main_ws.rows) == 1  # only the header row left -- evicted
    row = db_conn.execute("SELECT sheet_row_number FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["sheet_row_number"] is None
    assert len(job_log_ws.appended) == 1
