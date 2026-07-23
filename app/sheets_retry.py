"""Retry wrapper for Google Sheets API rate limits. Real failure mode hit
live: logging every job that clears the Filter Engine (hundreds at a time
on the first backlog run) means many individual write calls in a tight
loop, which blows through Sheets API's per-minute write quota
(gspread.exceptions.APIError, code 429) well before the job list is fully
logged. Retries with backoff rather than failing the whole run -- Sheets
quotas reset per-minute, so a short wait and retry is the standard fix.

Retryable codes match app.http_client.RETRYABLE_STATUS_CODES (429 plus
Google's own transient 5xx codes) -- widened after a live crash during an
uncapped enrichment run: a plain 503 ("service is currently unavailable",
not a quota error at all) wasn't retried under the original 429-only check,
so it took down the whole run instead of just backing off and continuing.
"""
from __future__ import annotations

import time
from typing import Callable, TypeVar

from gspread.exceptions import APIError

from app.http_client import RETRYABLE_STATUS_CODES

T = TypeVar("T")

_MAX_ATTEMPTS = 6
_BASE_DELAY_SECONDS = 20


def call_with_retry(fn: Callable[..., T], *args, **kwargs) -> T:
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except APIError as exc:
            if exc.code not in RETRYABLE_STATUS_CODES or attempt == _MAX_ATTEMPTS:
                raise
            time.sleep(_BASE_DELAY_SECONDS * attempt)
    raise AssertionError("unreachable")  # loop always returns or raises
