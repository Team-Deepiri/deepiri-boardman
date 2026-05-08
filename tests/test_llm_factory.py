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
    assert llm.kwargs["model"] == "anthropic/claude-3.5-sonnet"
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
