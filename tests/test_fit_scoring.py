import json

import pytest

from app.fit_scoring import FitScoreParseError, run_fit_scoring, score_job
from app.job_log import JOB_LOG_COLUMNS
from app.sheets import (
    JOB_ID_COL_INDEX,
    MAIN_SHEET_COLUMNS,
    MY_DECISION_AI_SCORE_PENDING,
    MY_DECISION_AI_SCORED,
    MY_DECISION_GO_SCORE,
    MY_DECISION_REJECT,
)
from tests.fakes import FakeAnthropicClient, FakeWorksheet


def _job_row(conn, **overrides):
    fields = {
        "company_id": None,
        "title": "Solutions Architect",
        "url": "https://example.com/1",
        "description": "Build integrations.",
        "status": "new",
        "visa_flag": "sponsors",
    }
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    cursor = conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", tuple(fields.values()))
    conn.commit()
    return cursor.lastrowid


def _score_response(score=70):
    return json.dumps(
        {"score": score, "matched_skills": ["Kafka"], "gaps": ["Kubernetes"], "summary": "ok"}
    )


def _ws_with_my_decision(*id_decision_pairs):
    """A Beacon worksheet with one row per (job_id, my_decision) pair."""
    rows = [MAIN_SHEET_COLUMNS]
    for job_id, my_decision in id_decision_pairs:
        row = [""] * len(MAIN_SHEET_COLUMNS)
        row[JOB_ID_COL_INDEX - 1] = str(job_id)
        row[MAIN_SHEET_COLUMNS.index("My Decision")] = my_decision
        rows.append(row)
    return FakeWorksheet(rows=rows)


def _requested_ws(*job_ids):
    """A Beacon worksheet with My Decision = "Go Score" for each job_id --
    the only way run_fit_scoring will consider a job."""
    return _ws_with_my_decision(*[(job_id, MY_DECISION_GO_SCORE) for job_id in job_ids])


def test_score_job_parses_structured_response():
    client = FakeAnthropicClient(_score_response(85))
    result, usage = score_job(client, "Solutions Architect", "Acme", "JD text", "Resume text")
    assert result["score"] == 85
    assert result["matched_skills"] == ["Kafka"]
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 50


def test_run_fit_scoring_returns_empty_without_main_ws(db_conn):
    # No Sheet to read My Decision from -- must not query the DB or spend
    # anything, not fall back to scoring everything.
    _job_row(db_conn, url="https://x/1", status="new", visa_flag="sponsors")
    client = FakeAnthropicClient(_score_response(70))
    settings = {"fit_score_threshold": 60, "daily_token_budget": None, "monthly_token_budget": None}

    result = run_fit_scoring(db_conn, client, "resume text", settings, main_ws=None)

    assert result["evaluated"] == 0
    assert result["rejected"] == 0
    assert client.messages.calls == []


def test_run_fit_scoring_ignores_unflagged_jobs_even_with_main_ws(db_conn):
    job_id = _job_row(db_conn, url="https://x/1", status="new", visa_flag="sponsors")
    # Row exists on Beacon but My Decision is blank ("New").
    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job_id)
    main_ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])

    client = FakeAnthropicClient(_score_response(70))
    settings = {"fit_score_threshold": 60, "daily_token_budget": None, "monthly_token_budget": None}

    result = run_fit_scoring(db_conn, client, "resume text", settings, main_ws=main_ws)

    assert result["evaluated"] == 0
    assert client.messages.calls == []


def test_run_fit_scoring_only_scores_flagged_new_jobs_with_visa_flag(db_conn):
    id1 = _job_row(db_conn, url="https://x/1", status="new", visa_flag="sponsors")
    id2 = _job_row(db_conn, url="https://x/2", status="new", visa_flag=None)
    id3 = _job_row(db_conn, url="https://x/3", status="filtered_out", visa_flag=None)

    client = FakeAnthropicClient(_score_response(70))
    settings = {"fit_score_threshold": 60, "daily_token_budget": None, "monthly_token_budget": None}
    main_ws = _requested_ws(id1, id2, id3)  # all flagged -- only id1 is actually eligible

    result = run_fit_scoring(db_conn, client, "resume text", settings, main_ws=main_ws)
    assert result["evaluated"] == 1
    assert result["scored"] == 1
    assert result["above_threshold"] == 1

    row = db_conn.execute("SELECT status FROM jobs WHERE url = 'https://x/1'").fetchone()
    assert row["status"] == "scored"

    fit_score_row = db_conn.execute(
        "SELECT score FROM fit_scores WHERE job_id = (SELECT id FROM jobs WHERE url = 'https://x/1')"
    ).fetchone()
    assert fit_score_row["score"] == 70


def test_run_fit_scoring_also_picks_up_ai_score_pending_for_retry(db_conn):
    # A prior run claimed this job (set AI Score Pending) but its API call
    # failed -- it must be retried automatically, not stuck forever.
    job_id = _job_row(db_conn, url="https://x/1", status="new", visa_flag="sponsors")
    main_ws = _ws_with_my_decision((job_id, MY_DECISION_AI_SCORE_PENDING))

    client = FakeAnthropicClient(_score_response(70))
    settings = {"fit_score_threshold": 60, "daily_token_budget": None, "monthly_token_budget": None}

    result = run_fit_scoring(db_conn, client, "resume text", settings, main_ws=main_ws)
    assert result["evaluated"] == 1
    assert result["scored"] == 1


def test_run_fit_scoring_claims_ai_score_pending_before_the_api_call(db_conn):
    job_id = _job_row(db_conn, url="https://x/1", status="new", visa_flag="sponsors")
    main_ws = _requested_ws(job_id)

    client = FakeAnthropicClient(_score_response(80))
    settings = {"fit_score_threshold": 60, "daily_token_budget": None, "monthly_token_budget": None}

    run_fit_scoring(db_conn, client, "resume text", settings, main_ws=main_ws)

    # Above threshold -> update_score() runs after the claim and flips it to
    # AI Scored, but the claim itself (AI Score Pending) must have happened
    # first -- verified indirectly by the final state being AI Scored, not
    # left at Go Score.
    row = dict(zip(MAIN_SHEET_COLUMNS, main_ws.rows[1]))
    assert row["My Decision"] == MY_DECISION_AI_SCORED


def test_run_fit_scoring_stays_ai_score_pending_on_parse_failure_for_retry(db_conn):
    job_id = _job_row(db_conn, url="https://x/1", status="new", visa_flag="sponsors")
    main_ws = _requested_ws(job_id)

    truncated = '{"score": 72, "matched_skills": ["Kafka"'
    client = FakeAnthropicClient(truncated)
    settings = {"fit_score_threshold": 60, "daily_token_budget": None, "monthly_token_budget": None}

    run_fit_scoring(db_conn, client, "resume text", settings, main_ws=main_ws)

    row = dict(zip(MAIN_SHEET_COLUMNS, main_ws.rows[1]))
    assert row["My Decision"] == MY_DECISION_AI_SCORE_PENDING  # claimed, then left in place on failure


def test_run_fit_scoring_below_threshold_still_scored_but_not_counted(db_conn):
    job_id = _job_row(db_conn, url="https://x/1", status="new", visa_flag="sponsors")

    client = FakeAnthropicClient(_score_response(40))
    settings = {"fit_score_threshold": 60, "daily_token_budget": None, "monthly_token_budget": None}

    result = run_fit_scoring(db_conn, client, "resume text", settings, main_ws=_requested_ws(job_id))
    assert result["scored"] == 1
    assert result["above_threshold"] == 0

    row = db_conn.execute("SELECT status FROM jobs WHERE url = 'https://x/1'").fetchone()
    assert row["status"] == "scored"


def test_run_fit_scoring_stops_when_budget_exceeded(db_conn):
    id1 = _job_row(db_conn, url="https://x/1", status="new", visa_flag="sponsors")
    id2 = _job_row(db_conn, url="https://x/2", status="new", visa_flag="sponsors", title="Sales Engineer")

    client = FakeAnthropicClient(_score_response(70))
    settings = {"fit_score_threshold": 60, "daily_token_budget": 0.0001, "monthly_token_budget": None}

    result = run_fit_scoring(db_conn, client, "resume text", settings, main_ws=_requested_ws(id1, id2))
    assert result["evaluated"] == 2
    assert result["scored"] == 1
    assert result["budget_exceeded"] is True


def test_run_fit_scoring_does_not_rescore_already_scored_jobs(db_conn):
    job_id = _job_row(db_conn, url="https://x/1", status="scored", visa_flag="sponsors")

    client = FakeAnthropicClient(_score_response(70))
    settings = {"fit_score_threshold": 60, "daily_token_budget": None, "monthly_token_budget": None}

    result = run_fit_scoring(db_conn, client, "resume text", settings, main_ws=_requested_ws(job_id))
    assert result["evaluated"] == 0


def test_run_fit_scoring_respects_limit(db_conn):
    ids = [_job_row(db_conn, url=f"https://x/{i}", status="new", visa_flag="sponsors") for i in range(5)]

    client = FakeAnthropicClient(_score_response(70))
    settings = {"fit_score_threshold": 60, "daily_token_budget": None, "monthly_token_budget": None}

    result = run_fit_scoring(db_conn, client, "resume text", settings, main_ws=_requested_ws(*ids), limit=2)
    assert result["evaluated"] == 2
    assert result["scored"] == 2

    remaining = db_conn.execute("SELECT COUNT(*) AS c FROM jobs WHERE status = 'new'").fetchone()["c"]
    assert remaining == 3


def test_score_job_raises_on_truncated_json():
    # real failure mode: max_tokens cuts generation off mid-JSON
    truncated = '{"score": 72, "matched_skills": ["Kafka", "MuleSoft'
    client = FakeAnthropicClient(truncated)
    with pytest.raises(FitScoreParseError) as exc_info:
        score_job(client, "Solutions Architect", "Acme", "JD text", "Resume text")
    assert exc_info.value.usage["input_tokens"] == 100


def test_run_fit_scoring_continues_past_a_parse_failure(db_conn):
    id1 = _job_row(db_conn, url="https://x/1", status="new", visa_flag="sponsors")
    id2 = _job_row(db_conn, url="https://x/2", status="new", visa_flag="sponsors")

    truncated = '{"score": 72, "matched_skills": ["Kafka"'
    client = FakeAnthropicClient([truncated, _score_response(80)])
    settings = {"fit_score_threshold": 60, "daily_token_budget": None, "monthly_token_budget": None}

    result = run_fit_scoring(db_conn, client, "resume text", settings, main_ws=_requested_ws(id1, id2))

    assert result["evaluated"] == 2
    assert result["failed"] == 1
    assert result["scored"] == 1
    assert result["above_threshold"] == 1

    statuses = {
        row["url"]: row["status"] for row in db_conn.execute("SELECT url, status FROM jobs")
    }
    assert statuses["https://x/1"] == "new"  # left as-is, not silently marked 'scored'
    assert statuses["https://x/2"] == "scored"


def test_run_fit_scoring_writes_job_log_below_threshold(db_conn):
    job_id = _job_row(db_conn, url="https://x/1", status="new", visa_flag="sponsors")

    client = FakeAnthropicClient(_score_response(40))
    settings = {"fit_score_threshold": 60, "daily_token_budget": None, "monthly_token_budget": None}
    job_log_ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS])

    run_fit_scoring(
        db_conn, client, "resume text", settings,
        job_log_ws=job_log_ws, main_ws=_requested_ws(job_id),
    )

    assert len(job_log_ws.appended) == 1
    row = dict(zip(JOB_LOG_COLUMNS, job_log_ws.appended[0]))
    assert row["Reason for Rejection"] == "Scored Below Threshold"
    assert row["Initial Fit Score"] == 40
    assert row["My Decision"] == MY_DECISION_AI_SCORED


def test_run_fit_scoring_updates_existing_job_log_row_in_place(db_conn):
    job_id = _job_row(db_conn, url="https://x/1", status="new", visa_flag="sponsors")

    client = FakeAnthropicClient(_score_response(40))
    settings = {"fit_score_threshold": 60, "daily_token_budget": None, "monthly_token_budget": None}
    existing_row = [""] * len(JOB_LOG_COLUMNS)
    existing_row[JOB_LOG_COLUMNS.index("Job ID")] = job_id
    existing_row[JOB_LOG_COLUMNS.index("Reason for Rejection")] = "Filtered Out - Seniority"
    job_log_ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS, existing_row])

    run_fit_scoring(
        db_conn, client, "resume text", settings,
        job_log_ws=job_log_ws, main_ws=_requested_ws(job_id),
    )

    # updated in place, not appended as a second row
    assert job_log_ws.appended == []
    row = dict(zip(JOB_LOG_COLUMNS, job_log_ws.rows[1]))
    assert row["Reason for Rejection"] == "Scored Below Threshold"
    assert row["Initial Fit Score"] == 40


def test_run_fit_scoring_no_job_log_write_above_threshold(db_conn):
    job_id = _job_row(db_conn, url="https://x/1", status="new", visa_flag="sponsors")

    client = FakeAnthropicClient(_score_response(80))
    settings = {"fit_score_threshold": 60, "daily_token_budget": None, "monthly_token_budget": None}
    job_log_ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS])

    run_fit_scoring(
        db_conn, client, "resume text", settings,
        job_log_ws=job_log_ws, main_ws=_requested_ws(job_id),
    )

    # above-threshold jobs stay on Beacon (score updated in place), no Job Log entry
    assert job_log_ws.appended == []


def test_run_fit_scoring_updates_beacon_score_above_threshold(db_conn):
    job_id = _job_row(db_conn, url="https://x/1", status="new", visa_flag="sponsors")
    main_ws = _requested_ws(job_id)

    client = FakeAnthropicClient(_score_response(80))
    settings = {"fit_score_threshold": 60, "daily_token_budget": None, "monthly_token_budget": None}

    run_fit_scoring(db_conn, client, "resume text", settings, main_ws=main_ws)

    row = dict(zip(MAIN_SHEET_COLUMNS, main_ws.rows[1]))
    assert row["Initial Fit Score"] == 80
    assert row["My Decision"] == MY_DECISION_AI_SCORED
    assert len(main_ws.rows) == 2  # still on Beacon


def test_run_fit_scoring_evicts_beacon_row_below_threshold(db_conn):
    job_id = _job_row(db_conn, url="https://x/1", status="new", visa_flag="sponsors")
    main_ws = _requested_ws(job_id)
    job_log_ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS])

    client = FakeAnthropicClient(_score_response(40))
    settings = {"fit_score_threshold": 60, "daily_token_budget": None, "monthly_token_budget": None}

    run_fit_scoring(db_conn, client, "resume text", settings, main_ws=main_ws, job_log_ws=job_log_ws)

    assert len(main_ws.rows) == 1  # only header row left -- evicted
    row = db_conn.execute("SELECT sheet_row_number FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["sheet_row_number"] is None
    assert len(job_log_ws.appended) == 1


def test_run_fit_scoring_processes_rejections_even_with_nothing_to_score(db_conn):
    job_id = _job_row(db_conn, url="https://x/1", status="new", visa_flag="sponsors")
    row_values = [""] * len(MAIN_SHEET_COLUMNS)
    row_values[JOB_ID_COL_INDEX - 1] = str(job_id)
    row_values[MAIN_SHEET_COLUMNS.index("My Decision")] = MY_DECISION_REJECT
    main_ws = FakeWorksheet(rows=[MAIN_SHEET_COLUMNS, row_values])
    job_log_ws = FakeWorksheet(rows=[JOB_LOG_COLUMNS])

    client = FakeAnthropicClient(_score_response(70))
    settings = {"fit_score_threshold": 60, "daily_token_budget": None, "monthly_token_budget": None}

    result = run_fit_scoring(
        db_conn, client, "resume text", settings, main_ws=main_ws, job_log_ws=job_log_ws,
    )

    assert result["rejected"] == 1
    assert result["evaluated"] == 0
    assert client.messages.calls == []  # no scoring spent on a rejected job
    assert len(main_ws.rows) == 1  # evicted from Beacon
    assert len(job_log_ws.appended) == 1
    row = dict(zip(JOB_LOG_COLUMNS, job_log_ws.appended[0]))
    assert row["Reason for Rejection"] == "Rejected (My Decision)"
    assert row["My Decision"] == MY_DECISION_REJECT

    db_row = db_conn.execute("SELECT sheet_row_number FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert db_row["sheet_row_number"] is None


def test_run_fit_scoring_rejection_and_scoring_run_in_the_same_pass(db_conn):
    rejected_id = _job_row(db_conn, url="https://x/1", status="new", visa_flag="sponsors")
    scored_id = _job_row(db_conn, url="https://x/2", status="new", visa_flag="sponsors")
    main_ws = _ws_with_my_decision(
        (rejected_id, MY_DECISION_REJECT), (scored_id, MY_DECISION_GO_SCORE),
    )

    client = FakeAnthropicClient(_score_response(80))
    settings = {"fit_score_threshold": 60, "daily_token_budget": None, "monthly_token_budget": None}

    result = run_fit_scoring(db_conn, client, "resume text", settings, main_ws=main_ws)

    assert result["rejected"] == 1
    assert result["evaluated"] == 1
    assert result["scored"] == 1
