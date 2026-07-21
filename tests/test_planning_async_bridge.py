"""Tests for the huddle sync/async bridge (boardman.planning.huddle.async_bridge).

These guard the fix for the review finding that the planning context providers
called ``asyncio.run()`` inside synchronous methods, which raises
``RuntimeError: asyncio.run() cannot be called from a running event loop`` when
the sync API is invoked from within an already-running loop.
"""

from __future__ import annotations

import asyncio

import pytest

from boardman.planning.huddle.async_bridge import run_sync


def test_run_sync_no_running_loop_returns_result() -> None:
    async def _coro() -> int:
        await asyncio.sleep(0)
        return 7

    assert run_sync(_coro()) == 7


def test_run_sync_from_within_running_loop_does_not_raise() -> None:
    """The whole point of the fix: safe to call from a running event loop."""

    async def _inner() -> str:
        await asyncio.sleep(0)
        return "bridged"

    async def _outer() -> str:
        # Simulates a sync provider method being reached from async code.
        return run_sync(_inner())

    assert asyncio.run(_outer()) == "bridged"


def test_run_sync_propagates_exceptions() -> None:
    async def _boom() -> None:
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        run_sync(_boom())
