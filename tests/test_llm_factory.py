from __future__ import annotations

import sys
import types

import pytest

from boardman.llm.factory import get_chat_model


class _DummyChatOpenAI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.mark.parametrize("provider", ["openrouter", "or"])
def test_factory_openrouter_returns_chatopenai_with_expected_config(monkeypatch, provider):
    fake_module = types.SimpleNamespace(ChatOpenAI=_DummyChatOpenAI)
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_module)

    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "llm_provider", provider)
    monkeypatch.setattr(bs.settings, "llm_model", "")
    monkeypatch.setattr(bs.settings, "openrouter_api_key", "openrouter-key")
    monkeypatch.setattr(bs.settings, "openrouter_base_url", "https://openrouter.ai/api/v1/")
    monkeypatch.setattr(bs.settings, "openrouter_referer", "https://example.test/app")
    monkeypatch.setattr(bs.settings, "openrouter_app_title", "Boardman")
    monkeypatch.setattr(bs.settings, "openai_api_key", "openai-key-that-should-not-be-used")

    llm = get_chat_model()

    assert isinstance(llm, _DummyChatOpenAI)
    # Free-tier default so nobody who forgets LLM_MODEL gets silently billed.
    assert llm.kwargs["model"] == "minimax/minimax-m3:free"
    assert llm.kwargs["api_key"] == "openrouter-key"
    assert llm.kwargs["base_url"] == "https://openrouter.ai/api/v1"
    assert llm.kwargs["default_headers"] == {
        "HTTP-Referer": "https://example.test/app",
        "X-Title": "Boardman",
    }


def test_factory_openrouter_omits_empty_optional_headers(monkeypatch):
    fake_module = types.SimpleNamespace(ChatOpenAI=_DummyChatOpenAI)
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_module)

    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "llm_provider", "openrouter")
    monkeypatch.setattr(bs.settings, "llm_model", "openai/gpt-4o-mini")
    monkeypatch.setattr(bs.settings, "openrouter_api_key", "openrouter-key")
    monkeypatch.setattr(bs.settings, "openrouter_base_url", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(bs.settings, "openrouter_referer", "")
    monkeypatch.setattr(bs.settings, "openrouter_app_title", "   ")

    llm = get_chat_model()

    assert isinstance(llm, _DummyChatOpenAI)
    assert llm.kwargs["model"] == "openai/gpt-4o-mini"
    assert llm.kwargs["api_key"] == "openrouter-key"
    assert llm.kwargs["default_headers"] is None


def test_factory_byok_override_bypasses_cache_and_shared_key(monkeypatch):
    """Bring-your-own-key (boardman/security/byok.py) must reach the actual LangChain
    model construction used by the tool-calling agent — this is the path
    run_tool_agent/iter_tool_agent build their model through."""
    from boardman.llm import factory as factory_mod

    fake_module = types.SimpleNamespace(ChatOpenAI=_DummyChatOpenAI)
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_module)

    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "llm_provider", "openrouter")
    monkeypatch.setattr(bs.settings, "llm_model", "some/shared-model")
    monkeypatch.setattr(bs.settings, "openrouter_api_key", "shared-default-key")
    factory_mod.clear_chat_model_cache()

    # Normal (no override) call populates the shared cache with the default key.
    default_llm = get_chat_model()
    assert default_llm.kwargs["api_key"] == "shared-default-key"

    # A BYOK call must get its OWN instance with the user's key — never the cached
    # shared-key instance, and never leak the override back into the shared cache.
    byok_llm = get_chat_model(provider_override="openrouter", api_key_override="users-own-key")
    assert byok_llm.kwargs["api_key"] == "users-own-key"
    assert byok_llm is not default_llm

    # A subsequent normal call still gets the shared-key instance, unaffected.
    again = get_chat_model()
    assert again.kwargs["api_key"] == "shared-default-key"
    factory_mod.clear_chat_model_cache()
