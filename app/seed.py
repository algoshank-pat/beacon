"""Company seed loader — upserts a YAML list of companies into the DB."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml

_COLUMNS = [
    "name",
    "board_url",
    "source_type",
    "industry",
    "company_size",
    "priority_tier",
    "is_favorite",
    "employee_count",
    "employee_count_range",
    "founded_year",
    "hq_location",
    "linkedin_company_url",
    "famous_product",
    "visa_sponsorship_history",
    "company_type",
    "funding_stage",
    "revenue_or_valuation",
    "revenue_valuation_source",
    "notes",
]


def load_seed_file(path: str | Path) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    companies = data.get("companies") or []
    for company in companies:
        if "name" not in company or not company["name"]:
            raise ValueError(f"Seed company entry missing required 'name': {company!r}")
    return companies


def seed_companies(conn: sqlite3.Connection, path: str | Path) -> dict[str, int]:
    companies = load_seed_file(path)
    inserted = 0
    updated = 0

    for company in companies:
        row = {col: company.get(col) for col in _COLUMNS}
        existing = conn.execute(
            "SELECT id FROM companies WHERE name = ?", (row["name"],)
        ).fetchone()

        if existing is None:
            columns = ", ".join(row.keys())
            placeholders = ", ".join(["?"] * len(row))
            conn.execute(
                f"INSERT INTO companies ({columns}) VALUES ({placeholders})",
                tuple(row.values()),
            )
            inserted += 1
        else:
            # COALESCE, not a blind overwrite: a real live bug wiped
            # employee_count/company_type/hq_location/etc. (all fields this
            # yaml file never sets per-company) back to NULL for every
            # already-enriched company also listed here, since re-running
            # this idempotent-by-design loader is a normal, expected
            # operation (e.g. adding one new company) -- it must never
            # regress data a totally separate process (app.enrichment)
            # already filled in. This file's own explicit values (name,
            # source_type, board_url, priority_tier, notes, ...) still take
            # priority when set, matching every other COALESCE-based writer
            # in this codebase (see app.enrichment._apply_enrichment).
            set_clause = ", ".join(f"{col} = COALESCE(?, {col})" for col in row if col != "name")
            values = [v for k, v in row.items() if k != "name"]
            conn.execute(
                f"UPDATE companies SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE name = ?",
                (*values, row["name"]),
            )
            updated += 1

    conn.commit()
    return {"inserted": inserted, "updated": updated}
