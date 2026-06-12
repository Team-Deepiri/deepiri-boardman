from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from boardman.database.models import AgentSession, Base


async def _memory_engine_and_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory


@pytest.mark.asyncio
async def test_tool_trace_persisted_on_success(monkeypatch):
    import boardman.agent.service as agent_svc
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", True)

    async def fake_run_tool_agent(*args: Any, **kwargs: Any):
        return (
            "done",
            [
                {
                    "tool_name": "plaky_list_tasks",
                    "args": {"status": "open"},
                    "status": "ok",
                    "result_summary": '{"ok": true}',
                }
            ],
        )

    monkeypatch.setattr(agent_svc, "run_tool_agent", fake_run_tool_agent)
    engine, factory = await _memory_engine_and_factory()
    try:
        async with factory() as session:
            reply, sid = await agent_svc.run_agent_chat(
                session,
                message="Use tools",
                session_id=None,
                repo="o/r",
                allow_writes=False,
                use_tools=True,
            )
            await session.commit()
        assert reply == "done"
        async with factory() as session:
            q = (
                select(AgentSession)
                .where(AgentSession.session_id == sid)
                .options(selectinload(AgentSession.messages))
            )
            ag = (await session.execute(q)).scalar_one()
            assistant_rows = [m for m in ag.messages if m.role == "assistant"]
            assert assistant_rows
            trace_json = assistant_rows[-1].tool_calls_json
            assert trace_json
            trace = json.loads(trace_json)
            assert isinstance(trace, list)
            assert trace[0]["tool_name"] == "plaky_list_tasks"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_tool_trace_persisted_on_error(monkeypatch):
    import boardman.agent.service as agent_svc
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", True)

    async def fake_run_tool_agent(*args: Any, **kwargs: Any):
        raise RuntimeError("tool boom")

    async def fake_safe_plain_chat(**kwargs: Any) -> str:
        return "fallback"

    monkeypatch.setattr(agent_svc, "run_tool_agent", fake_run_tool_agent)
    monkeypatch.setattr(agent_svc, "_safe_plain_chat", fake_safe_plain_chat)

    engine, factory = await _memory_engine_and_factory()
    try:
        async with factory() as session:
            reply, sid = await agent_svc.run_agent_chat(
                session,
                message="Use tools please",
                session_id=None,
                repo="o/r",
                allow_writes=False,
                use_tools=True,
            )
            await session.commit()
        assert reply == "fallback"

        async with factory() as session:
            q = (
                select(AgentSession)
                .where(AgentSession.session_id == sid)
                .options(selectinload(AgentSession.messages))
            )
            ag = (await session.execute(q)).scalar_one()
            assistant_rows = [m for m in ag.messages if m.role == "assistant"]
            trace_json = assistant_rows[-1].tool_calls_json
            trace = json.loads(trace_json or "[]")
            assert trace
            assert trace[0]["status"] == "error"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_session_history_handles_null_tool_calls_json(monkeypatch):
    import boardman.agent.service as agent_svc
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", False)

    async def fake_chat_complete(messages, **kwargs):
        return "simple"

    monkeypatch.setattr(agent_svc, "chat_complete", fake_chat_complete)

    engine, factory = await _memory_engine_and_factory()
    try:
        async with factory() as session:
            _reply, sid = await agent_svc.run_agent_chat(
                session,
                message="hi",
                session_id=None,
                repo="o/r",
                use_tools=False,
            )
            await session.commit()
        async with factory() as session:
            hist = await agent_svc.get_session_history(session, sid)
            assert len(hist) == 2
            assert hist[-1]["role"] == "assistant"
    finally:
        await engine.dispose()
