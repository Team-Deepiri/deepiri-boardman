import json
from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base
from boardman.database.session import get_db
from boardman.main import create_app


@pytest.fixture
async def memory_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_agent_chat_stream_with_tools_mocked(monkeypatch, memory_db):
    import boardman.agent.service as agent_svc
    import boardman.settings as bs

    # Enable tools
    monkeypatch.setattr(bs.settings, "agent_langchain_tools", True)

    async def fake_iter_tool_agent(*args, **kwargs):
        assert "trace_out" in kwargs
        yield "tool-"
        yield "output"

    monkeypatch.setattr(agent_svc, "iter_tool_agent", fake_iter_tool_agent)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with memory_db() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    # Stream endpoint opens DB sessions via routes.agent.async_session.
    import boardman.routes.agent as agent_routes

    monkeypatch.setattr(agent_routes, "async_session", memory_db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # We use a POST to /agent/chat/stream with use_tools=True
        response = await client.post(
            "/api/v1/agent/chat/stream",
            json={"message": "hello tools", "use_tools": True, "allow_writes": False},
        )
        assert response.status_code == 200

        events = []
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

        # We expect: session, token (tool-), token (output), done
        types = [e["type"] for e in events]
        assert "session" in types
        assert "token" in types
        assert "done" in types

        tokens = "".join([e["text"] for e in events if e["type"] == "token"])
        assert tokens == "tool-output"


@pytest.mark.asyncio
async def test_agent_chat_stream_bulk_preview_downgrades_writes(monkeypatch, memory_db):
    import boardman.agent.service as agent_svc
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", True)
    monkeypatch.setattr(bs.settings, "agent_require_confirm_bulk", True)

    captured: list[bool] = []

    async def fake_iter_tool_agent(*args, **kwargs):
        captured.append(bool(kwargs.get("allow_writes")))
        yield "ok"

    monkeypatch.setattr(agent_svc, "iter_tool_agent", fake_iter_tool_agent)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with memory_db() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/agent/chat/stream",
            json={
                "message": "Please organize and bulk move tasks to done",
                "use_tools": True,
                "allow_writes": True,
            },
        )
        assert response.status_code == 200
        assert captured and captured[0] is False


@pytest.mark.asyncio
async def test_agent_chat_stream_error_payload_is_provider_specific(monkeypatch, memory_db):
    import boardman.agent.service as agent_svc
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", True)

    async def fake_iter_tool_agent(*args, **kwargs):
        req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        resp = httpx.Response(
            401,
            request=req,
            text='{"error":{"message":"Invalid API key"}}',
        )
        raise httpx.HTTPStatusError("upstream 401", request=req, response=resp)
        yield  # pragma: no cover

    monkeypatch.setattr(agent_svc, "iter_tool_agent", fake_iter_tool_agent)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with memory_db() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/agent/chat/stream",
            json={
                "message": "hello tools",
                "use_tools": True,
                "provider": "openai",
                "model": "gpt-4o-mini",
            },
        )
        assert response.status_code == 200
        events = []
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

        err_events = [e for e in events if e.get("type") == "error"]
        assert err_events
        msg = err_events[0].get("message", "")
        assert "**Provider:** `openai`" in msg
        assert "**Model:** `gpt-4o-mini`" in msg
        assert "OPENAI_API_KEY" in msg
