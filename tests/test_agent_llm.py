"""Agent HTTP path with LLM mocked (no LangChain tool loop)."""

import pytest
from httpx import ASGITransport, AsyncClient

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
