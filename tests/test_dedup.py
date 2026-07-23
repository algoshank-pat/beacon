from app.dedup import find_fuzzy_duplicate


def _insert_job(conn, company_id, title, location, status="new"):
    conn.execute(
        "INSERT INTO jobs (company_id, title, url, location, status) VALUES (?, ?, ?, ?, ?)",
        (company_id, title, f"https://example.com/{title}-{location}", location, status),
    )
    conn.commit()


def test_finds_near_identical_title_same_location(db_conn):
    company_id = 1
    db_conn.execute("INSERT INTO companies (id, name) VALUES (1, 'Acme')")
    _insert_job(db_conn, company_id, "Senior Solutions Architect", "Remote - US")

    match = find_fuzzy_duplicate(db_conn, company_id, "Sr Solutions Architect", "Remote - US")
    assert match is not None


def test_no_match_for_different_titles(db_conn):
    company_id = 1
    db_conn.execute("INSERT INTO companies (id, name) VALUES (1, 'Acme')")
    _insert_job(db_conn, company_id, "Senior Solutions Architect", "Remote - US")

    match = find_fuzzy_duplicate(db_conn, company_id, "Staff Software Engineer", "Remote - US")
    assert match is None


def test_no_match_for_different_locations(db_conn):
    company_id = 1
    db_conn.execute("INSERT INTO companies (id, name) VALUES (1, 'Acme')")
    _insert_job(db_conn, company_id, "Senior Solutions Architect", "New York, NY")

    match = find_fuzzy_duplicate(db_conn, company_id, "Senior Solutions Architect", "London, UK")
    assert match is None


def test_ignores_jobs_already_marked_duplicate(db_conn):
    company_id = 1
    db_conn.execute("INSERT INTO companies (id, name) VALUES (1, 'Acme')")
    _insert_job(db_conn, company_id, "Senior Solutions Architect", "Remote - US", status="duplicate")

    match = find_fuzzy_duplicate(db_conn, company_id, "Senior Solutions Architect", "Remote - US")
    assert match is None


def test_scoped_to_company(db_conn):
    db_conn.execute("INSERT INTO companies (id, name) VALUES (1, 'Acme')")
    db_conn.execute("INSERT INTO companies (id, name) VALUES (2, 'Other Co')")
    _insert_job(db_conn, 1, "Senior Solutions Architect", "Remote - US")

    match = find_fuzzy_duplicate(db_conn, 2, "Senior Solutions Architect", "Remote - US")
    assert match is None
