"""LLM helpers and provider wiring (mocked HTTP; no live models required)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from boardman.llm.completion import chat_complete, parse_json_tasks
from boardman.llm.ollama_autodetect import pick_preferred_ollama_model


def test_pick_preferred_ollama_model_order():
    assert pick_preferred_ollama_model(["llama3:8b", "mistral:7b"]) == "llama3:8b"
    assert pick_preferred_ollama_model(["mistral:7b", "qwen2.5:7b"]) == "qwen2.5:7b"
    assert pick_preferred_ollama_model(["zebra:latest", "alpha:1"]) == "alpha:1"


class TestParseJsonTasks:
    def test_plain_array(self):
        raw = '[{"title": "a", "description": "b", "priority": "low"}]'
        out = parse_json_tasks(raw)
        assert isinstance(out, list)
        assert out[0]["title"] == "a"

    def test_array_embedded_in_text(self):
        raw = 'Here you go:\n[{"title": "x"}]\nThanks.'
        out = parse_json_tasks(raw)
        assert out == [{"title": "x"}]

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="valid JSON"):
            parse_json_tasks("not json at all")


def _install_fake_httpx_client(monkeypatch, post_json_response: dict):
    """Patch httpx.AsyncClient used by boardman.llm.completion."""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.timeout = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json=None, headers=None):
            r = MagicMock()
            r.raise_for_status = MagicMock()
            r.json = MagicMock(return_value=post_json_response)
            return r

    monkeypatch.setattr("boardman.llm.completion.httpx.AsyncClient", FakeClient)


@pytest.mark.asyncio
async def test_chat_complete_ollama_parses_message(monkeypatch):
    _install_fake_httpx_client(
        monkeypatch,
        {"message": {"content": "  hello from model  "}},
    )
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "ollama_base_url", "http://127.0.0.1:11434")

    out = await chat_complete([{"role": "user", "content": "hi"}], provider="ollama", model="llama3:8b")
    assert out == "hello from model"


@pytest.mark.asyncio
async def test_chat_complete_ollama_legacy_response_field(monkeypatch):
    _install_fake_httpx_client(monkeypatch, {"response": "legacy"})
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "ollama_base_url", "http://127.0.0.1:11434")

    out = await chat_complete([{"role": "user", "content": "hi"}], provider="ollama", model="m")
    assert out == "legacy"


@pytest.mark.asyncio
async def test_chat_complete_unknown_provider():
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        await chat_complete([{"role": "user", "content": "x"}], provider="not-a-real-provider", model="m")


@pytest.mark.asyncio
async def test_chat_complete_anthropic_requires_api_key(monkeypatch):
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "anthropic_api_key", "")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        await chat_complete([{"role": "user", "content": "x"}], provider="anthropic", model="claude-3-haiku")


@pytest.mark.asyncio
async def test_chat_complete_openai_requires_api_key(monkeypatch):
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "openai_api_key", "")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        await chat_complete([{"role": "user", "content": "x"}], provider="openai", model="gpt-4o-mini")


@pytest.mark.asyncio
async def test_chat_complete_gemini_requires_api_key(monkeypatch):
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "gemini_api_key", "")
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        await chat_complete([{"role": "user", "content": "x"}], provider="gemini", model="gemini-2.0-flash")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ollama_live_chat_complete():
    """Set BOARDMAN_AI_LIVE=1 and run Ollama with at least one model. Skips otherwise."""
    import os

    if os.environ.get("BOARDMAN_AI_LIVE") != "1":
        pytest.skip("Live check: export BOARDMAN_AI_LIVE=1 and start Ollama")

    from boardman.llm.ollama_autodetect import effective_ollama_model

    model = effective_ollama_model(None)
    text = await chat_complete(
        [{"role": "user", "content": 'Reply with exactly: OK'}],
        provider="ollama",
        model=model,
        timeout=120.0,
    )
    assert "OK" in text.upper()
