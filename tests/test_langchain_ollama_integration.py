"""
Live checks: LangChain ChatOllama -> Ollama (optional: tool agent uses create_agent + same stack).

Skipped when Ollama is down or LLM_MODEL is not pulled (see conftest pytest_collection_modifyitems).

Run:
  poetry run pytest tests/test_langchain_ollama_integration.py -v

Host hitting Docker Ollama:
  OLLAMA_BASE_URL=http://127.0.0.1:11434 poetry run pytest tests/test_langchain_ollama_integration.py -v
"""

from __future__ import annotations

import os

import httpx
import pytest
from langchain_core.messages import HumanMessage

OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")

pytestmark = pytest.mark.live_ollama


def test_ollama_tags_list_models(require_ollama):
    r = httpx.get(f"{OLLAMA_BASE}/api/tags", timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data.get("models"), list)


@pytest.mark.asyncio
async def test_langchain_chataollama_ainvoke(require_ollama, require_ollama_model, monkeypatch):
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", False)
    from boardman.llm.factory import get_chat_model

    llm = get_chat_model()
    msg = await llm.ainvoke(
        [HumanMessage(content="Reply with exactly this token: LANGCHAIN_OLLAMA_PING")],
    )
    text = (getattr(msg, "content", None) or str(msg)).strip()
    assert len(text) > 0
    assert "error" not in text.lower()[:200]
    upper = text.upper()
    assert "LANGCHAIN" in upper or "PING" in upper or "OLLAMA" in upper or len(text) < 500


