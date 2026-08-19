from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.agent.fast_path import maybe_fast_path
from boardman.database.models import Base


@pytest.mark.asyncio
async def test_current_repo_is_answered_without_an_llm() -> None:
    result = await maybe_fast_path(
        "What repo am I currently working with?",
        repo="deepiri/boardman",
        board_id=None,
        group_id=None,
    )

    assert result is not None
    assert result.intent == "current_repo"
    assert "deepiri/boardman" in result.reply


@pytest.mark.asyncio
async def test_open_task_list_is_compact_and_read_only(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class FakePlaky:
        async def get_tasks(self, *, status: str, board_id: str):
            calls.append((status, board_id))
            return {
                "ok": True,
                "tasks": [
                    {"id": "42", "title": "Fix webhook retry", "status": "open"},
                ],
            }

    monkeypatch.setattr("boardman.agent.fast_path.PlakyClient", FakePlaky)
    result = await maybe_fast_path(
        "list open tasks",
        repo="deepiri/boardman",
        board_id="board-1",
        group_id="group-1",
    )

    assert result is not None
    assert result.intent == "list_open_tasks"
    assert "Fix webhook retry" in result.reply
    assert calls == [("open", "board-1")]


@pytest.mark.asyncio
async def test_write_intent_does_not_take_read_only_task_fast_path(monkeypatch) -> None:
    class ExplodingPlaky:
        async def get_tasks(self, **_kwargs):
            raise AssertionError("write request must continue through the agent")

    monkeypatch.setattr("boardman.agent.fast_path.PlakyClient", ExplodingPlaky)
    result = await maybe_fast_path(
        "create open tasks",
        repo="deepiri/boardman",
        board_id="board-1",
        group_id=None,
    )

    assert result is None


@pytest.mark.asyncio
async def test_service_keeps_repo_scope_for_a_follow_up_fast_path(monkeypatch) -> None:
    import boardman.agent.service as agent_service
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", False)
    monkeypatch.setattr(
        agent_service,
        "_resolve_placement",
        lambda board, group, _repo: (board, group, ""),
    )

    async def no_plaky_suffix(_board, _group, note=""):
        return ""

    monkeypatch.setattr(agent_service, "_plaky_system_suffix", no_plaky_suffix)
    calls = 0

    async def fake_chat(_messages, **_kwargs):
        nonlocal calls
        calls += 1
        return "normal reply"

    monkeypatch.setattr(agent_service, "chat_complete", fake_chat)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with factory() as session:
            _, sid = await agent_service.run_agent_chat(
                session,
                message="hello",
                session_id=None,
                repo="org/repo",
            )
            await session.commit()
            reply, _ = await agent_service.run_agent_chat(
                session,
                message="What repo am I currently working with?",
                session_id=sid,
                repo=None,
            )
            await session.commit()
        assert "org/repo" in reply
        assert calls == 1, "the follow-up should bypass the LLM"
    finally:
        await engine.dispose()
