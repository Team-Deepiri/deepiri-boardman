
import pytest
from httpx import ASGITransport, AsyncClient
from boardman.main import create_app
from boardman.database.session import get_db
from boardman.database.models import Base
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
import json
from typing import AsyncIterator, Any, List

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
    
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # We use a POST to /agent/chat/stream with use_tools=True
        response = await client.post(
            "/api/v1/agent/chat/stream",
            json={
                "message": "hello tools",
                "use_tools": True,
                "allow_writes": False
            }
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
