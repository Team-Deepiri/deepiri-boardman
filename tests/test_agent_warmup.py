"""The first question after a restart should not pay for the caches every question uses."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_warmup_fills_the_roster_and_every_routed_board(monkeypatch) -> None:
    from boardman.agent import warmup

    fetched: list[str] = []
    roster_calls: list[int] = []

    async def fake_bundle(board_id: str):
        fetched.append(board_id)
        return {"ok": True}

    monkeypatch.setattr("boardman.plaky.board_schema.fetch_board_schema_bundle", fake_bundle)
    monkeypatch.setattr(
        "boardman.assignment.config.load_team_assignments",
        lambda *_a, **_k: roster_calls.append(1),
    )
    monkeypatch.setattr(warmup, "_board_ids", lambda: ["269028", "269031"])

    await warmup.warm_agent_caches()

    assert sorted(fetched) == ["269028", "269031"]
    assert roster_calls, "the roster was not warmed"


@pytest.mark.asyncio
async def test_a_failing_warmup_never_breaks_startup(monkeypatch) -> None:
    """It is an optimisation. If Plaky is down at boot the service must still start."""
    from boardman.agent import warmup

    async def boom(_board_id: str):
        raise RuntimeError("plaky is down")

    def boom_sync(*_a, **_k):
        raise RuntimeError("github is down")

    monkeypatch.setattr("boardman.plaky.board_schema.fetch_board_schema_bundle", boom)
    monkeypatch.setattr("boardman.assignment.config.load_team_assignments", boom_sync)
    monkeypatch.setattr(warmup, "_board_ids", lambda: ["269028"])

    await warmup.warm_agent_caches()  # must not raise


def test_board_ids_are_deduped_and_bounded() -> None:
    from boardman.agent import warmup

    ids = warmup._board_ids()
    assert len(ids) == len(set(ids))
    assert len(ids) <= 4
