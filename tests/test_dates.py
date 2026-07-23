from datetime import datetime, timezone

from app.dates import format_central, parse_datetime


def test_parse_datetime_handles_iso_with_offset():
    dt = parse_datetime("2026-06-18T17:04:21-04:00")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 6 and dt.day == 18


def test_parse_datetime_handles_z_suffix():
    dt = parse_datetime("2026-07-01T08:25:45Z")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_datetime_handles_sqlite_timestamp_as_utc():
    dt = parse_datetime("2026-07-02 23:27:31")
    assert dt is not None
    assert dt.tzinfo == timezone.utc


def test_parse_datetime_returns_none_for_garbage():
    assert parse_datetime("not a date") is None


def test_parse_datetime_returns_none_for_empty():
    assert parse_datetime(None) is None
    assert parse_datetime("") is None


def test_format_central_converts_utc_to_central_and_formats_mmddyyyy():
    # 2026-07-02 23:27:31 UTC -> Central (CDT, UTC-5 in July) -> 2026-07-02 18:27
    result = format_central("2026-07-02 23:27:31")
    assert result == "07022026 18:27"


def test_format_central_handles_datetime_object():
    dt = datetime(2026, 1, 15, 20, 0, 0, tzinfo=timezone.utc)
    # January is CST (UTC-6) -> 14:00 Central
    assert format_central(dt) == "01152026 14:00"


def test_format_central_empty_for_none_or_blank():
    assert format_central(None) == ""
    assert format_central("") == ""


def test_format_central_passes_through_unparseable_input():
    assert format_central("not a real date") == "not a real date"
