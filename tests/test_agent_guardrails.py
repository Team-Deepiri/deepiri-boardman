from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.agent.guardrails import (
    WRITE_TOOLS,
    has_confirm_token,
    is_write_tool,
    looks_like_board_organize_request,
)
from boardman.agent.tools import build_all_tools
from boardman.database.models import Base


async def _memory_engine_and_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory


def test_is_write_tool_classification():
    assert is_write_tool("plaky_create_task") is True
    assert is_write_tool("plaky_update_task") is True
    assert is_write_tool("plaky_link_prs") is True
    assert is_write_tool("plaky_list_tasks") is False
    assert is_write_tool("unknown_tool_name") is False


def test_write_tools_matches_build_all_tools_delta():
    ro = frozenset(t.name for t in build_all_tools(allow_writes=False))
    rw = frozenset(t.name for t in build_all_tools(allow_writes=True))
    assert frozenset(rw - ro) == WRITE_TOOLS
    assert WRITE_TOOLS.isdisjoint(ro)


def test_organize_detection_and_confirm_token():
    assert looks_like_board_organize_request("please organize board and reorder tasks")
    assert looks_like_board_organize_request("bulk move tasks to done")
    assert looks_like_board_organize_request("Organize and reorder everything")
    assert looks_like_board_organize_request("please organize the sprint")
    assert not looks_like_board_organize_request("what is this repo about?")
    assert not looks_like_board_organize_request("reorder alphabetically")
    assert not looks_like_board_organize_request("don't organize the backlog")
    assert not looks_like_board_organize_request("never bulk move tasks")
    assert looks_like_board_organize_request("don't worry, please organize the board")
    assert has_confirm_token("yes, apply now")
    assert has_confirm_token("confirm")
    assert has_confirm_token("go ahead")
    assert has_confirm_token("approved")
    assert has_confirm_token("yes please apply")
    assert has_confirm_token("YES, APPLY")  # normalized / case-insensitive regex
    assert not has_confirm_token("preview only")


@pytest.mark.asyncio
async def test_setting_toggle_keeps_writes_enabled_when_confirm_not_required(monkeypatch):
    import boardman.agent.service as agent_svc
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_langchain_tools", True)
    monkeypatch.setattr(bs.settings, "agent_require_confirm_bulk", False)
    captured: list[bool] = []

    async def fake_run_tool_agent(*args: Any, **kwargs: Any):
        captured.append(bool(kwargs.get("allow_writes")))
        return "ok", [{"tool_name": "plaky_update_task", "status": "ok"}]

    monkeypatch.setattr(agent_svc, "run_tool_agent", fake_run_tool_agent)

    engine, factory = await _memory_engine_and_factory()
    try:
        async with factory() as session:
            reply, _ = await agent_svc.run_agent_chat(
                session,
                message="Organize and reorder everything",
                session_id=None,
                repo="o/r",
                allow_writes=True,
                use_tools=True,
            )
            await session.commit()
        assert "preview mode" not in reply.lower()
        assert captured[-1] is True
    finally:
        await engine.dispose()
