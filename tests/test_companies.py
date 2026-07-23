from app.companies import get_or_create_company


def test_creates_new_company_when_no_match(db_conn):
    company_id = get_or_create_company(db_conn, "Acme Corp", "adzuna")
    row = db_conn.execute("SELECT name, source_type FROM companies WHERE id = ?", (company_id,)).fetchone()
    assert row["name"] == "Acme Corp"
    assert row["source_type"] == "adzuna"


def test_returns_existing_company_case_and_whitespace_insensitive(db_conn):
    first_id = get_or_create_company(db_conn, "Acme Corp", "adzuna")
    second_id = get_or_create_company(db_conn, "  acme   corp  ", "adzuna")
    assert first_id == second_id

    count = db_conn.execute("SELECT COUNT(*) AS c FROM companies").fetchone()["c"]
    assert count == 1


def test_does_not_overwrite_seeded_company_source_type(db_conn):
    db_conn.execute(
        "INSERT INTO companies (name, source_type, priority_tier) VALUES ('Boomi', 'ashby', 'A')"
    )
    db_conn.commit()

    company_id = get_or_create_company(db_conn, "Boomi", "adzuna")
    row = db_conn.execute("SELECT source_type, priority_tier FROM companies WHERE id = ?", (company_id,)).fetchone()
    assert row["source_type"] == "ashby"
    assert row["priority_tier"] == "A"
