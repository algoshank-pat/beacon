from dataclasses import dataclass

import pytest

from app.llm_provider import LLMProviderError, build_llm_client


@dataclass
class _FakeSettings:
    llm_provider: str = "anthropic"
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None


def test_build_llm_client_builds_anthropic_by_default():
    settings = _FakeSettings(llm_provider="anthropic", anthropic_api_key="fake-key")
    client = build_llm_client(settings)
    assert type(client).__module__.startswith("anthropic")


def test_build_llm_client_raises_when_anthropic_key_missing():
    settings = _FakeSettings(llm_provider="anthropic", anthropic_api_key=None)
    with pytest.raises(LLMProviderError, match="ANTHROPIC_API_KEY"):
        build_llm_client(settings)


def test_build_llm_client_builds_gemini_when_configured():
    settings = _FakeSettings(llm_provider="gemini", gemini_api_key="fake-key")
    client = build_llm_client(settings)
    assert type(client).__module__.startswith("google")


def test_build_llm_client_raises_when_gemini_key_missing():
    settings = _FakeSettings(llm_provider="gemini", gemini_api_key=None)
    with pytest.raises(LLMProviderError, match="GEMINI_API_KEY"):
        build_llm_client(settings)


def test_build_llm_client_raises_on_unknown_provider():
    settings = _FakeSettings(llm_provider="chatgpt", anthropic_api_key="fake-key")
    with pytest.raises(LLMProviderError, match="chatgpt"):
        build_llm_client(settings)


def test_build_llm_client_defaults_to_anthropic_when_provider_blank():
    settings = _FakeSettings(llm_provider="", anthropic_api_key="fake-key")
    client = build_llm_client(settings)
    assert type(client).__module__.startswith("anthropic")
