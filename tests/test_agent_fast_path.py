from __future__ import annotations

from types import SimpleNamespace

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
@pytest.mark.parametrize(
    "question",
    [
        "what's on the board right now?",
        "whats on the board",
        "what is in the plaky board currently",
        "what does the board look like",
        "anything left on the board?",
    ],
)
async def test_asking_what_is_on_the_board_is_not_answered_with_routing_ids(
    question: str, monkeypatch
) -> None:
    """It is a question about the work. Answering "board 269028, group 933385" is a
    non-answer, and the routing pattern used to swallow every one of these.

    get_routing is patched so the fast path CAN answer: without it the routing lookup
    returns None for this fake repo and the test would pass with the guard removed.
    """
    monkeypatch.setattr(
        "boardman.agent.fast_path.get_routing",
        lambda *_a, **_k: SimpleNamespace(
            plaky_board_id="269028", plaky_group_id="933385", plaky_table="deepiri-boardman"
        ),
    )
    result = await maybe_fast_path(
        question, repo="deepiri/boardman", board_id="269028", group_id="933385"
    )
    assert result is None or result.intent != "repo_routing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "which board does this repo go to?",
        "what board is this project routed to",
        "which group does this task belong in",
        # Says "in the board", but it is asking where work is FILED, not what is on it.
        "which group on the board should this task go in?",
    ],
)
async def test_genuine_routing_questions_still_take_the_fast_path(question, monkeypatch) -> None:
    monkeypatch.setattr(
        "boardman.agent.fast_path.get_routing",
        lambda *_a, **_k: SimpleNamespace(
            plaky_board_id="269028", plaky_group_id="933385", plaky_table="deepiri-boardman"
        ),
    )
    result = await maybe_fast_path(
        question, repo="deepiri/boardman", board_id="269028", group_id="933385"
    )
    assert result is not None and result.intent == "repo_routing"
    assert "269028" in result.reply


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
        lambda board, group, _repo, **_k: (board, group, ""),
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
