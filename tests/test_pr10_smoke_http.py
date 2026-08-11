"""PR #10 smoke: HTTP agent path sees read-only tool registry (plaky_review_board)."""

import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.agent.tools import build_all_tools
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
async def test_pr10_smoke_agent_chat_stream_readonly_registry_includes_plaky_review_board(
    monkeypatch, memory_db
):
    """POST /agent/chat/stream with use_tools and allow_writes=false builds tools that include plaky_review_board."""
    import boardman.agent.service as agent_svc
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", True)

    captured: list[tuple[bool, list[str]]] = []

    async def fake_iter_tool_agent(*args, allow_writes=False, **kwargs):
        tools = build_all_tools(allow_writes=allow_writes)
        names = sorted(t.name for t in tools if getattr(t, "name", None))
        captured.append((bool(allow_writes), names))
        assert "plaky_review_board" in names
        assert allow_writes is False
        yield "pr10-smoke-ok"

    monkeypatch.setattr(agent_svc, "iter_tool_agent", fake_iter_tool_agent)

    async def override_get_db():
        async with memory_db() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/agent/chat/stream",
            json={
                "message": "Run plaky_review_board on my board for duplicates.",
                "use_tools": True,
                "allow_writes": False,
            },
        )
    assert response.status_code == 200
    assert captured and captured[0][0] is False
    assert "plaky_review_board" in captured[0][1]

    events = []
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    types = [e["type"] for e in events]
    assert "session" in types
    assert "token" in types
    assert "done" in types
    tokens = "".join(e["text"] for e in events if e["type"] == "token")
    assert tokens == "pr10-smoke-ok"
