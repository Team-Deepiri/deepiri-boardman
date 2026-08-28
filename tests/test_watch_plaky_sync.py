"""The sync watcher reads its labels from the board, not from a copy in the script.

Hardcoding Plaky's status/type/priority option names meant a status the team added or
renamed printed as a bare id, and a diff quietly stopped making sense. The board already
knows its own vocabulary, so ask it (Sorge review, PR #88). The hardcoded table survives
only as the fallback for when that call fails.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_watcher():
    """scripts/ is not a package, so load the module straight off disk."""
    spec = importlib.util.spec_from_file_location(
        "watch_plaky_sync", ROOT / "scripts" / "watch_plaky_sync.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bundle(fields: list[dict[str, Any]]) -> dict[str, Any]:
    return {"ok": True, "normalized": {"board_name": "b", "groups": [], "fields": fields}}


@pytest.mark.asyncio
async def test_option_labels_come_from_the_live_board_schema(monkeypatch) -> None:
    watcher = _load_watcher()

    async def fake_bundle(_board_id: str) -> dict[str, Any]:
        return _bundle(
            [
                {
                    "name": "Status",
                    "type": "STATUS",
                    "options": [
                        {"id": "0", "name": "NEEDS ASSIGNED"},
                        {"id": "12", "name": "Ready For Deploy"},  # added after this script
                    ],
                },
                {"name": "Priority", "type": "STATUS", "options": [{"id": "0", "name": "Urgent"}]},
            ]
        )

    monkeypatch.setattr("boardman.plaky.board_schema.fetch_board_schema_bundle", fake_bundle)
    names = await watcher.option_names_for_board("269031")

    assert names["Status"]["12"] == "Ready For Deploy", "a new option needs no code change"
    assert names["Priority"]["0"] == "Urgent", "a renamed option wins over the hardcoded copy"
    # A column the board did not describe still falls back rather than vanishing.
    assert names["Type"] == watcher.FALLBACK_OPTION_NAMES["Type"]
    # A column the board described only PARTIALLY keeps the ids it did not mention,
    # rather than printing them raw.
    assert names["Status"]["2"] == watcher.FALLBACK_OPTION_NAMES["Status"]["2"]
    assert names["Priority"]["3"] == watcher.FALLBACK_OPTION_NAMES["Priority"]["3"]


@pytest.mark.asyncio
async def test_a_failed_schema_read_degrades_to_the_hardcoded_labels(monkeypatch) -> None:
    watcher = _load_watcher()

    async def boom(_board_id: str) -> dict[str, Any]:
        raise RuntimeError("plaky unreachable")

    monkeypatch.setattr("boardman.plaky.board_schema.fetch_board_schema_bundle", boom)
    assert await watcher.option_names_for_board("269031") == watcher.FALLBACK_OPTION_NAMES


@pytest.mark.asyncio
async def test_an_empty_schema_degrades_to_the_hardcoded_labels(monkeypatch) -> None:
    watcher = _load_watcher()

    async def empty(_board_id: str) -> dict[str, Any]:
        return {"ok": False, "normalized": None}

    monkeypatch.setattr("boardman.plaky.board_schema.fetch_board_schema_bundle", empty)
    assert await watcher.option_names_for_board("269031") == watcher.FALLBACK_OPTION_NAMES


@pytest.mark.asyncio
async def test_the_fallback_table_is_never_mutated(monkeypatch) -> None:
    """The overlay must copy: one board's live labels must not leak into another's."""
    watcher = _load_watcher()
    before = {k: dict(v) for k, v in watcher.FALLBACK_OPTION_NAMES.items()}

    async def fake_bundle(_board_id: str):
        return _bundle(
            [
                {
                    "name": "Status",
                    "type": "STATUS",
                    "options": [{"id": "0", "name": "SOMETHING ELSE ENTIRELY"}],
                }
            ]
        )

    monkeypatch.setattr("boardman.plaky.board_schema.fetch_board_schema_bundle", fake_bundle)
    names = await watcher.option_names_for_board("269031")

    assert names["Status"]["0"] == "SOMETHING ELSE ENTIRELY"
    assert before == watcher.FALLBACK_OPTION_NAMES


@pytest.mark.asyncio
async def test_the_snapshot_renders_status_with_the_boards_own_labels(monkeypatch) -> None:
    watcher = _load_watcher()

    class FakeClient:
        async def list_board_items(self, _board: str, max_pages: int = 3) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "id": "t1",
                        "name": "Fix retry crash",
                        "groupId": "g1",
                        "fields": [
                            {"title": "Status", "type": "STATUS", "value": "12"},
                            {"title": "Assignee", "type": "PERSON", "value": {"assignedUsers": []}},
                        ],
                    }
                ]
            }

    monkeypatch.setattr("boardman.plaky.client.PlakyClient", lambda *_a, **_k: FakeClient())
    rows = await watcher.snapshot("269031", "g1", {}, {"Status": {"12": "Ready For Deploy"}})

    assert rows["t1"]["Status"] == "Ready For Deploy"


@pytest.mark.asyncio
async def test_an_unknown_option_id_is_shown_as_itself_not_dropped(monkeypatch) -> None:
    """Better a visible raw id than a label that silently means something else."""
    watcher = _load_watcher()

    class FakeClient:
        async def list_board_items(self, _board: str, max_pages: int = 3) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "id": "t1",
                        "name": "x",
                        "groupId": "g1",
                        "fields": [{"title": "Status", "type": "STATUS", "value": "99"}],
                    }
                ]
            }

    monkeypatch.setattr("boardman.plaky.client.PlakyClient", lambda *_a, **_k: FakeClient())
    rows = await watcher.snapshot("269031", "g1", {}, {"Status": {"0": "NEEDS ASSIGNED"}})
    assert rows["t1"]["Status"] == "99"
