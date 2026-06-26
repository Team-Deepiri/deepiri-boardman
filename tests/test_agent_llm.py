"""Agent HTTP path with LLM mocked (no LangChain tool loop)."""

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from boardman.database.session import init_db
from boardman.main import create_app


@pytest.mark.asyncio
async def test_agent_chat_plain_llm_path(monkeypatch):
    """AGENT_LANGCHAIN_TOOLS off → chat_complete only."""
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", False)

    async def fake_chat_complete(messages, **kwargs):
        assert any(m.get("role") == "user" for m in messages)
        return "mocked-assistant-reply"

    import boardman.agent.service as agent_svc

    monkeypatch.setattr(agent_svc, "chat_complete", fake_chat_complete)

    # httpx ASGI transport doesn't run app lifespan; ensure DB tables exist.
    await init_db()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/agent/chat",
            json={"message": "Say hello", "repo": "org/demo"},
        )
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert data.get("reply") == "mocked-assistant-reply"
    assert data.get("session_id")


@pytest.mark.asyncio
async def test_agent_chat_plain_error_is_provider_specific(monkeypatch):
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", False)

    async def fake_chat_complete(messages, **kwargs):
        req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        resp = httpx.Response(
            404,
            request=req,
            text='{"error":{"message":"No route for model"}}',
        )
        raise httpx.HTTPStatusError("upstream 404", request=req, response=resp)

    import boardman.agent.service as agent_svc

    monkeypatch.setattr(agent_svc, "chat_complete", fake_chat_complete)

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/agent/chat",
            json={
                "message": "Say hello",
                "repo": "org/demo",
                "provider": "openrouter",
                "model": "minimax/minimax-m2.5:free",
            },
        )
    assert r.status_code == 200
    data = r.json()
    reply = data.get("reply") or ""
    assert "**Provider:** `openrouter`" in reply
    assert "**Model:** `minimax/minimax-m2.5:free`" in reply
    assert "provider-prefixed model IDs" in reply
    assert "ollama list" not in reply.lower()
