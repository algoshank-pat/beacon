"""Detects mentions of AWS/GCP/Azure in a job's title+description text.
Computed once at ingest time (see app.ingest.upsert_job) from whatever text
is already stored -- no extra network fetch, same cheap-at-ingest-time
design as app.location_state. Adzuna-sourced descriptions are sometimes
truncated to 500 characters (the same truncation that previously affected
visa-scan and salary extraction) -- a cloud mention past that cutoff is
simply missed, a known, accepted gap rather than a bug worth a full-page
refresh pass for right now.
"""
from __future__ import annotations

import re

_AWS_RE = re.compile(r"\b(?:aws|amazon\s+web\s+services)\b", re.IGNORECASE)
_GCP_RE = re.compile(r"\b(?:gcp|google\s+cloud(?:\s+platform)?)\b", re.IGNORECASE)
_AZURE_RE = re.compile(r"\bazure\b", re.IGNORECASE)


def resolve_cloud_platforms(text: str | None) -> str | None:
    """Returns a comma-separated string of every cloud platform mentioned
    (always in AWS, GCP, Azure order, regardless of the order they appear in
    the text), or None if none are mentioned."""
    if not text:
        return None
    found = []
    if _AWS_RE.search(text):
        found.append("AWS")
    if _GCP_RE.search(text):
        found.append("GCP")
    if _AZURE_RE.search(text):
        found.append("Azure")
    return ", ".join(found) if found else None
