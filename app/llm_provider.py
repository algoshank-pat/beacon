"""Builds the LLM client used for visa classification and fit scoring,
based on `Settings.llm_provider` ("anthropic", the default, or "gemini").
The one place that imports either SDK and checks the right API key -- every
call site (app.cli, app.pipeline) calls build_llm_client() instead of
duplicating that branch four times.

Both providers' classify/score functions (app.visa_scan.haiku_classify/
gemini_classify, app.fit_scoring.score_job/gemini_score_job) already share
an identical (client, ...) -> (result, usage) contract, so the only thing
that needs to vary per call site is which client object gets built here and
which `provider` string gets threaded through to run_visa_scan/
run_fit_scoring's own dispatch (see VISA_CLASSIFIERS/FIT_SCORE_PROVIDERS in
those modules).
"""
from __future__ import annotations

SUPPORTED_PROVIDERS = ("anthropic", "gemini")


class LLMProviderError(Exception):
    """Raised when the configured provider's API key isn't set, or
    LLM_PROVIDER itself isn't a recognized value."""


def build_llm_client(settings):
    provider = (settings.llm_provider or "anthropic").lower()

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise LLMProviderError("ANTHROPIC_API_KEY not set")
        import anthropic

        return anthropic.Anthropic(api_key=settings.anthropic_api_key)

    if provider == "gemini":
        if not settings.gemini_api_key:
            raise LLMProviderError("GEMINI_API_KEY not set")
        from google import genai

        return genai.Client(api_key=settings.gemini_api_key)

    raise LLMProviderError(f"Unknown LLM_PROVIDER {provider!r} -- must be one of {SUPPORTED_PROVIDERS}")
