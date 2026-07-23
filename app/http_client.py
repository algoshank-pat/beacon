"""HTTP wrapper with retry/backoff, shared by every ingestion source."""
from __future__ import annotations

import time

import requests

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Adzuna's landing pages (and many ATS/job-board pages) return 403 to a bare
# requests.get() with no headers; a realistic browser header set is enough
# to pass -- confirmed against a real listing. Shared here (not private to
# one module) since both app.salary_extraction and app.link_check need it
# for the same reason: fetching a job's own posting page directly.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class RequestFailedError(Exception):
    """Raised when a request exhausts all retry attempts."""


def request_with_retry(
    method: str,
    url: str,
    *,
    session=requests,
    max_attempts: int = 3,
    backoff_base: float = 0.5,
    sleep_fn=time.sleep,
    **kwargs,
) -> requests.Response:
    """Issue an HTTP request, retrying transient failures with exponential backoff.

    Retries on connection/timeout errors and on RETRYABLE_STATUS_CODES. Any other
    response (including 404, which callers use for closed-job detection) is
    returned immediately without retrying.
    """
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = session.request(method, url, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if attempt < max_attempts:
                sleep_fn(backoff_base * (2 ** (attempt - 1)))
                continue
            raise RequestFailedError(
                f"{method} {url} failed after {max_attempts} attempts: {exc}"
            ) from exc

        if response.status_code in RETRYABLE_STATUS_CODES and attempt < max_attempts:
            sleep_fn(backoff_base * (2 ** (attempt - 1)))
            continue

        if response.status_code in RETRYABLE_STATUS_CODES:
            raise RequestFailedError(
                f"{method} {url} failed after {max_attempts} attempts: "
                f"last status {response.status_code}"
            )

        return response

    raise RequestFailedError(f"{method} {url} failed after {max_attempts} attempts: {last_exc}")


def get_json(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    session=None,
    timeout: int = 15,
    max_attempts: int = 3,
):
    """GET a URL and return parsed JSON, raising on non-2xx status after retries."""
    kwargs: dict = {"params": params, "timeout": timeout}
    if headers is not None:
        kwargs["headers"] = headers
    if session is not None:
        kwargs["session"] = session
    response = request_with_retry("GET", url, max_attempts=max_attempts, **kwargs)
    response.raise_for_status()
    return response.json()
