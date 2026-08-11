"""Request-scoped context for LangChain tools (DB session + agent session row)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy.ext.asyncio import AsyncSession

_db_session: ContextVar[AsyncSession | None] = ContextVar("agent_tool_db_session", default=None)
_agent_session_pk: ContextVar[int | None] = ContextVar("agent_tool_agent_session_pk", default=None)
_plaky_board_id: ContextVar[str | None] = ContextVar("agent_tool_plaky_board_id", default=None)
_plaky_group_id: ContextVar[str | None] = ContextVar("agent_tool_plaky_group_id", default=None)


def get_tool_db_session() -> AsyncSession | None:
    return _db_session.get()


def get_agent_session_pk() -> int | None:
    return _agent_session_pk.get()


def get_context_plaky_board_id() -> str | None:
    return _plaky_board_id.get()


def get_context_plaky_group_id() -> str | None:
    return _plaky_group_id.get()


@asynccontextmanager
async def agent_tool_context(
    db: AsyncSession,
    agent_session_pk: int,
    plaky_board_id: str | None,
    plaky_group_id: str | None,
) -> AsyncIterator[None]:
    t_db = _db_session.set(db)
    t_pk = _agent_session_pk.set(agent_session_pk)
    t_b = _plaky_board_id.set((plaky_board_id or "").strip() or None)
    t_g = _plaky_group_id.set((plaky_group_id or "").strip() or None)
    try:
        yield
    finally:
        _db_session.reset(t_db)
        _agent_session_pk.reset(t_pk)
        _plaky_board_id.reset(t_b)
        _plaky_group_id.reset(t_g)
