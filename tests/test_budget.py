import pytest

from app.budget import BudgetTracker, estimate_cost_usd, get_spend_this_month, get_spend_today


def test_estimate_cost_usd_sonnet():
    cost = estimate_cost_usd("claude-sonnet-5", 1_000_000, 1_000_000)
    assert cost == pytest.approx(2.00 + 10.00)


def test_estimate_cost_usd_haiku():
    cost = estimate_cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000)
    assert cost == pytest.approx(1.00 + 5.00)


def test_estimate_cost_usd_unknown_model_returns_zero():
    assert estimate_cost_usd("unknown-model", 1000, 1000) == 0.0


def test_get_spend_today_sums_only_todays_runs(db_conn):
    db_conn.execute(
        "INSERT INTO workflow_runs (run_type, status, estimated_cost_usd, started_at) "
        "VALUES ('main_pipeline', 'completed', 0.50, datetime('now'))"
    )
    db_conn.execute(
        "INSERT INTO workflow_runs (run_type, status, estimated_cost_usd, started_at) "
        "VALUES ('main_pipeline', 'completed', 5.00, '2020-01-01 00:00:00')"
    )
    db_conn.commit()

    assert get_spend_today(db_conn) == pytest.approx(0.50)


def test_get_spend_this_month_excludes_other_months(db_conn):
    db_conn.execute(
        "INSERT INTO workflow_runs (run_type, status, estimated_cost_usd, started_at) "
        "VALUES ('main_pipeline', 'completed', 1.25, datetime('now'))"
    )
    db_conn.execute(
        "INSERT INTO workflow_runs (run_type, status, estimated_cost_usd, started_at) "
        "VALUES ('main_pipeline', 'completed', 9.00, '2020-01-01 00:00:00')"
    )
    db_conn.commit()

    assert get_spend_this_month(db_conn) == pytest.approx(1.25)


def test_budget_tracker_unlimited_when_no_budget_set(db_conn):
    tracker = BudgetTracker(db_conn, None, None)
    assert tracker.remaining_daily() == float("inf")
    assert tracker.has_budget()


def test_budget_tracker_tracks_session_spend(db_conn):
    tracker = BudgetTracker(db_conn, daily_budget=1.0, monthly_budget=10.0)
    assert tracker.has_budget()
    tracker.record_spend(0.90)
    assert tracker.has_budget()
    tracker.record_spend(0.20)
    assert not tracker.has_budget()


def test_budget_tracker_accounts_for_prior_spend_today(db_conn):
    db_conn.execute(
        "INSERT INTO workflow_runs (run_type, status, estimated_cost_usd, started_at) "
        "VALUES ('main_pipeline', 'completed', 0.95, datetime('now'))"
    )
    db_conn.commit()

    tracker = BudgetTracker(db_conn, daily_budget=1.0, monthly_budget=10.0)
    assert tracker.remaining_daily() == pytest.approx(0.05)
