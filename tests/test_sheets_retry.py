from unittest.mock import Mock

import pytest
from gspread.exceptions import APIError

from app.sheets_retry import call_with_retry


def _rate_limit_error():
    response = Mock()
    response.json.return_value = {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}}
    return APIError(response)


def _service_unavailable_error():
    response = Mock()
    response.json.return_value = {"error": {"code": 503, "message": "Service unavailable", "status": "UNAVAILABLE"}}
    return APIError(response)


def _non_retryable_error():
    response = Mock()
    response.json.return_value = {"error": {"code": 403, "message": "Permission denied", "status": "PERMISSION_DENIED"}}
    return APIError(response)


def test_call_with_retry_returns_result_on_success():
    fn = Mock(return_value="ok")
    assert call_with_retry(fn, 1, key="value") == "ok"
    fn.assert_called_once_with(1, key="value")


def test_call_with_retry_retries_past_429_then_succeeds(monkeypatch):
    monkeypatch.setattr("app.sheets_retry.time.sleep", lambda _: None)
    fn = Mock(side_effect=[_rate_limit_error(), _rate_limit_error(), "ok"])
    assert call_with_retry(fn) == "ok"
    assert fn.call_count == 3


def test_call_with_retry_retries_past_503_then_succeeds(monkeypatch):
    # A plain "service unavailable" isn't a quota error at all, but it's the
    # same transient-failure class app.http_client already retries for every
    # other API in this pipeline -- a live crash during an uncapped
    # enrichment run (503, not 429) showed this module hadn't matched that.
    monkeypatch.setattr("app.sheets_retry.time.sleep", lambda _: None)
    fn = Mock(side_effect=[_service_unavailable_error(), "ok"])
    assert call_with_retry(fn) == "ok"
    assert fn.call_count == 2


def test_call_with_retry_reraises_non_retryable_code_immediately(monkeypatch):
    monkeypatch.setattr("app.sheets_retry.time.sleep", lambda _: None)
    fn = Mock(side_effect=_non_retryable_error())
    with pytest.raises(APIError):
        call_with_retry(fn)
    assert fn.call_count == 1


def test_call_with_retry_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr("app.sheets_retry.time.sleep", lambda _: None)
    fn = Mock(side_effect=_rate_limit_error())
    with pytest.raises(APIError):
        call_with_retry(fn)
    assert fn.call_count == 6
