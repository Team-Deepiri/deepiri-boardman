"""Request-scoped Plaky board_id + group_id (Plaky has no separate "table" API concept)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import AsyncIterator, Optional

from boardman.settings import settings

_board_var: ContextVar[Optional[str]] = ContextVar("plaky_board_id", default=None)
_group_var: ContextVar[Optional[str]] = ContextVar("plaky_group_id", default=None)


def context_board_id() -> Optional[str]:
    v = _board_var.get()
    if v and v.strip():
        return v.strip()
    d = (settings.plaky_default_board_id or "").strip()
    return d or None


def context_group_id() -> Optional[str]:
    v = _group_var.get()
    if v and v.strip():
        return v.strip()
    d = (settings.plaky_default_group_id or "").strip()
    return d or None


@asynccontextmanager
async def plaky_placement_context(
    board_id: Optional[str],
    group_id: Optional[str],
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
