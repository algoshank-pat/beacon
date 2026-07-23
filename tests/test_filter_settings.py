from app.filter_settings import get_active_keywords, get_filter_settings


def test_get_filter_settings_coerces_types(db_conn):
    db_conn.executemany(
        "INSERT INTO filter_settings (key, value) VALUES (?, ?)",
        [
            ("fit_score_threshold", "60"),
            ("require_visa_sponsorship", "false"),
            ("daily_token_budget", "1.5"),
            ("company_priority_min", "B"),
            ("employee_count_max", None),
        ],
    )
    db_conn.commit()

    settings = get_filter_settings(db_conn)
    assert settings["fit_score_threshold"] == 60
    assert settings["require_visa_sponsorship"] is False
    assert settings["daily_token_budget"] == 1.5
    assert settings["company_priority_min"] == "B"
    assert settings["employee_count_max"] is None


def test_get_active_keywords_filters_inactive(db_conn):
    db_conn.execute(
        "INSERT INTO filter_keywords (category, keyword, is_active) VALUES ('seniority', 'senior', 1)"
    )
    db_conn.execute(
        "INSERT INTO filter_keywords (category, keyword, is_active) VALUES ('seniority', 'junior', 0)"
    )
    db_conn.commit()

    assert get_active_keywords(db_conn, "seniority") == ["senior"]
