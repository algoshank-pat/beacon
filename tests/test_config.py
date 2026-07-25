from app.config import _resolve_llm_provider


def test_resolve_llm_provider_defaults_to_anthropic_when_neither_key_set(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert _resolve_llm_provider() == "anthropic"


def test_resolve_llm_provider_defaults_to_anthropic_when_both_keys_set(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
    assert _resolve_llm_provider() == "anthropic"


def test_resolve_llm_provider_infers_gemini_when_only_gemini_key_set(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
    assert _resolve_llm_provider() == "gemini"


def test_resolve_llm_provider_infers_anthropic_when_only_anthropic_key_set(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert _resolve_llm_provider() == "anthropic"


def test_resolve_llm_provider_explicit_env_var_wins_even_with_only_other_key_set(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert _resolve_llm_provider() == "gemini"
