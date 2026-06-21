"""Request-scoped Plaky board_id + group_id (Plaky has no separate "table" API concept)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

_board_var: ContextVar[str | None] = ContextVar("plaky_board_id", default=None)
_group_var: ContextVar[str | None] = ContextVar("plaky_group_id", default=None)


def context_board_id() -> str | None:
    v = _board_var.get()
    if v and v.strip():
        return v.strip()
    return None


def context_group_id() -> str | None:
    v = _group_var.get()
    if v and v.strip():
        return v.strip()
    return None


@asynccontextmanager
async def plaky_placement_context(
    board_id: str | None,
    group_id: str | None,
) -> AsyncIterator[None]:
    b = (board_id or "").strip() or None
    g = (group_id or "").strip() or None
    tb = _board_var.set(b)
    tg = _group_var.set(g)
    try:
        yield
    finally:
        _board_var.reset(tb)
        _group_var.reset(tg)
