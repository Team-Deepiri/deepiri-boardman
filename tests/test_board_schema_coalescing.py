"""One fetch per board, and never remember a failure.

The cache lock only ever covered the dict read and write, so N concurrent misses on the
same board each made their own pair of Plaky calls. A burst is exactly when that hurts:
the cold first question after a restart is the one that fans out.

The second rule matters more than the speed. A failed schema read used to be cached for
ninety seconds, and a task created in that window resolves no field keys at all — the
board accepts the item and drops every column on it.
"""

from __future__ import annotations

import asyncio

import pytest

from boardman.plaky import board_schema
from boardman.plaky.board_schema import clear_board_schema_cache, fetch_board_schema_bundle

BOARD = "269028"


@pytest.fixture(autouse=True)
def _clean():
    clear_board_schema_cache()
    yield
    clear_board_schema_cache()


class _FakeClient:
    """Counts calls and is deliberately slow, so a stampede is visible."""

    calls = 0
    ok = True

    def __init__(self) -> None:
        pass

    async def list_groups(self, _bid: str):
        type(self).calls += 1
        await asyncio.sleep(0.05)
        return {"ok": self.ok, "groups": [{"id": "1", "name": "g"}] if self.ok else []}

    async def get_board(self, _bid: str):
        await asyncio.sleep(0.05)
        return {"ok": self.ok, "board": {"name": "Bots", "fields": []} if self.ok else None}

    def _public_root(self) -> str:
        return ""  # no v1/public root, so the item-stub enrichment pass is skipped


@pytest.fixture()
def fake_plaky(monkeypatch):
    _FakeClient.calls = 0
    _FakeClient.ok = True
    monkeypatch.setattr("boardman.plaky.client.PlakyClient", _FakeClient)

    async def no_redis(*_a, **_k):
        return None

    monkeypatch.setattr("boardman.cache.agent_redis.agent_redis_get_json", no_redis)
    monkeypatch.setattr("boardman.cache.agent_redis.agent_redis_set_json", no_redis)
    monkeypatch.setattr(board_schema.settings, "plaky_board_schema_cache_ttl_seconds", 90.0)
    yield _FakeClient


@pytest.mark.asyncio
async def test_concurrent_misses_make_one_fetch(fake_plaky) -> None:
    results = await asyncio.gather(*(fetch_board_schema_bundle(BOARD) for _ in range(6)))
    assert fake_plaky.calls == 1, f"{fake_plaky.calls} fetches for 6 concurrent callers"
    assert all(r["ok"] for r in results)


@pytest.mark.asyncio
async def test_different_boards_are_not_serialised_behind_each_other(fake_plaky) -> None:
    """A per-board lock, not a global one: two boards must overlap."""
    started = asyncio.get_event_loop().time()
    await asyncio.gather(
        fetch_board_schema_bundle("111"),
        fetch_board_schema_bundle("222"),
        fetch_board_schema_bundle("333"),
    )
    elapsed = asyncio.get_event_loop().time() - started
    assert fake_plaky.calls == 3
    # Each fake fetch sleeps ~0.1s; serialised would be ~0.3s.
    assert elapsed < 0.25, f"boards were serialised ({elapsed:.2f}s for 3)"


@pytest.mark.asyncio
async def test_a_warm_cache_makes_no_call_at_all(fake_plaky) -> None:
    await fetch_board_schema_bundle(BOARD)
    assert fake_plaky.calls == 1
    for _ in range(4):
        await fetch_board_schema_bundle(BOARD)
    assert fake_plaky.calls == 1


@pytest.mark.asyncio
async def test_a_failed_read_is_not_remembered(fake_plaky) -> None:
    """Otherwise the next ninety seconds of task creation write no fields."""
    fake_plaky.ok = False
    first = await fetch_board_schema_bundle(BOARD)
    assert first["ok"] is False
    assert fake_plaky.calls == 1

    fake_plaky.ok = True
    second = await fetch_board_schema_bundle(BOARD)
    assert second["ok"] is True, "a retry must be allowed to succeed"
    assert fake_plaky.calls == 2


@pytest.mark.asyncio
async def test_an_empty_board_id_never_reaches_the_client(fake_plaky) -> None:
    out = await fetch_board_schema_bundle("")
    assert out["ok"] is False
    assert fake_plaky.calls == 0
