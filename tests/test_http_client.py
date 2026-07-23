import pytest
import requests

from app.http_client import RequestFailedError, get_json, request_with_retry
from tests.fakes import FakeResponse, FakeSession


def test_returns_response_on_success():
    session = FakeSession([FakeResponse(200, {"ok": True})])
    response = request_with_retry("GET", "https://example.com", session=session, sleep_fn=lambda s: None)
    assert response.status_code == 200
    assert len(session.calls) == 1


def test_retries_on_connection_error_then_succeeds():
    session = FakeSession([requests.ConnectionError("boom"), FakeResponse(200, {"ok": True})])
    response = request_with_retry("GET", "https://example.com", session=session, sleep_fn=lambda s: None)
    assert response.status_code == 200
    assert len(session.calls) == 2


def test_retries_on_retryable_status_then_succeeds():
    session = FakeSession([FakeResponse(503), FakeResponse(200, {"ok": True})])
    response = request_with_retry("GET", "https://example.com", session=session, sleep_fn=lambda s: None)
    assert response.status_code == 200
    assert len(session.calls) == 2


def test_raises_after_exhausting_attempts():
    session = FakeSession([FakeResponse(500), FakeResponse(500), FakeResponse(500)])
    with pytest.raises(RequestFailedError):
        request_with_retry(
            "GET", "https://example.com", session=session, max_attempts=3, sleep_fn=lambda s: None
        )
    assert len(session.calls) == 3


def test_does_not_retry_404():
    session = FakeSession([FakeResponse(404)])
    response = request_with_retry("GET", "https://example.com", session=session, sleep_fn=lambda s: None)
    assert response.status_code == 404
    assert len(session.calls) == 1


def test_get_json_returns_parsed_body():
    session = FakeSession([FakeResponse(200, {"jobs": []})])
    assert get_json("https://example.com", session=session) == {"jobs": []}


def test_get_json_raises_for_status_on_error():
    session = FakeSession([FakeResponse(404)])
    with pytest.raises(requests.HTTPError):
        get_json("https://example.com", session=session)
