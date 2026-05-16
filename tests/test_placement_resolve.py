"""Plaky placement via API lists + rank_plaky_rows (shared with agent tools)."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from boardman.plaky.placement_resolve import resolve_scan_placement


@pytest.mark.asyncio
async def test_resolve_scan_placement_board_name_query(monkeypatch: pytest.MonkeyPatch) -> None:
    import boardman.plaky.placement_resolve as pr

    async def fake_match_boards(_q: str) -> Dict[str, Any]:
        return {
            "list_ok": True,
            "message": None,
            "matches": [{"id": "b1", "name": "Engineering", "score": 1000}],
            "best": {"id": "b1", "name": "Engineering", "score": 1000},
        }

    async def fake_match_groups(board_id: str, q: str) -> Dict[str, Any]:
        assert board_id == "b1"
        assert q == "Sprint"
        return {
            "list_ok": True,
            "message": None,
            "matches": [{"id": "g9", "name": "Sprint", "score": 500}],
            "best": {"id": "g9", "name": "Sprint", "score": 500},
        }

    monkeypatch.setattr(pr, "match_boards", fake_match_boards)
    monkeypatch.setattr(pr, "match_groups", fake_match_groups)

    bid, gid, meta = await resolve_scan_placement(
        board_id="",
        group_id="",
        board_name_query="Engineering",
        group_name_query="Sprint",
    )
    assert bid == "b1"
    assert gid == "g9"
    assert "board_match" in meta
    assert "group_match" in meta


@pytest.mark.asyncio
async def test_resolve_scan_placement_uses_plaky_table_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import boardman.plaky.placement_resolve as pr

    async def fake_match_groups(board_id: str, q: str) -> Dict[str, Any]:
        assert board_id == "board-x"
        assert q == "AI Bugs / What to DO"
        return {
            "list_ok": True,
            "message": None,
            "matches": [],
            "best": {"id": "grp-1", "name": "AI Bugs / What to DO", "score": 1000},
        }

    monkeypatch.setattr(pr, "match_groups", fake_match_groups)

    bid, gid, meta = await resolve_scan_placement(
        board_id="board-x",
        group_id="",
        group_fallback_name="AI Bugs / What to DO",
    )
    assert bid == "board-x"
    assert gid == "grp-1"
    assert "group_match" in meta


@pytest.mark.asyncio
async def test_resolve_scan_placement_board_query_no_strong_match(monkeypatch: pytest.MonkeyPatch) -> None:
    import boardman.plaky.placement_resolve as pr

    async def fake_match_boards(_q: str) -> Dict[str, Any]:
        return {
            "list_ok": True,
            "message": None,
            "matches": [{"id": "z", "name": "Unrelated", "score": 100}],
            "best": None,
        }

    monkeypatch.setattr(pr, "match_boards", fake_match_boards)

    bid, gid, meta = await resolve_scan_placement(
        board_id="",
        group_id="",
        board_name_query="zzzznopelikely",
    )
    assert bid == ""
    assert gid == ""
    assert meta.get("board_error")
