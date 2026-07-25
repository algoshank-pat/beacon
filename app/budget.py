"""Token spend tracking and the daily/monthly budget guardrail.

Checked before each LLM call (fit scoring's Sonnet calls, and could be reused
for Haiku visa classification too) — if the running total would exceed
daily_token_budget/monthly_token_budget, remaining LLM-dependent steps for the
run are paused and an alert is logged. No separate alert channel: this logs to
step_logs now; M8 wires the same alert into the Sheet's System Log tab.
"""
from __future__ import annotations

import sqlite3

# claude-api pricing reference, per 1M tokens. Sonnet 5 is at its introductory
# rate through 2026-08-31 -- update to $3/$15 after that date.
#
# Gemini rates confirmed live against Google's own pricing page (not a
# third-party estimate). gemini-3.5-flash-lite replaces the original
# gemini-2.5-flash-lite pick -- see app.visa_scan's GEMINI_FLASH_MODEL
# comment for why. gemini-2.5-pro is unverified against a real key (every
# live test attempt hit a 429 quota error before reaching the model --
# quota exhaustion, not the 404 "no longer available" error the flash
# models gave, so it's not confirmed broken the way they were).
PRICING_PER_MTOK = {
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},  # ≤200k-token-prompt rate; rises above that
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = PRICING_PER_MTOK.get(model)
    if rates is None:
        return 0.0
    return (input_tokens / 1_000_000) * rates["input"] + (output_tokens / 1_000_000) * rates["output"]


def get_spend_today(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(estimated_cost_usd), 0) AS total FROM workflow_runs
        WHERE date(started_at) = date('now')
        """
    ).fetchone()
    return row["total"]


def get_spend_this_month(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(estimated_cost_usd), 0) AS total FROM workflow_runs
        WHERE strftime('%Y-%m', started_at) = strftime('%Y-%m', 'now')
        """
    ).fetchone()
    return row["total"]


class BudgetTracker:
    """Tracks spend for the current run against the daily/monthly ceiling.

    Prior runs' spend is read once at construction; spend from LLM calls made
    during *this* run is accumulated in memory via record_spend(), since it
    isn't in workflow_runs until the run finishes.
    """

    def __init__(self, conn: sqlite3.Connection, daily_budget: float | None, monthly_budget: float | None):
        self.daily_budget = daily_budget
        self.monthly_budget = monthly_budget
        self.session_spend = 0.0
        self._daily_spend_before = get_spend_today(conn)
        self._monthly_spend_before = get_spend_this_month(conn)

    def remaining_daily(self) -> float:
        if self.daily_budget is None:
            return float("inf")
        return self.daily_budget - self._daily_spend_before - self.session_spend

    def remaining_monthly(self) -> float:
        if self.monthly_budget is None:
            return float("inf")
        return self.monthly_budget - self._monthly_spend_before - self.session_spend

    def has_budget(self) -> bool:
        return self.remaining_daily() > 0 and self.remaining_monthly() > 0

    def record_spend(self, cost_usd: float) -> None:
        self.session_spend += cost_usd
