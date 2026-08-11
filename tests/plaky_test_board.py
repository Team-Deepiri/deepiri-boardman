"""
Live Plaky pytest helpers: resolve the Boardman Test Board and a named group (default \"Sprint 2\").

Override discovery with:
  PLAKY_BOARDMAN_TEST_BOARD_ID, PLAKY_BOARDMAN_TEST_GROUP_ID
  PLAKY_BOARDMAN_TEST_GROUP_NAME (default group display name; default: Sprint 2)
"""

from __future__ import annotations

import os
from typing import Any

from boardman.plaky.client import PlakyClient

BOARDMAN_TEST_BOARD_NAME = "Boardman Test Board"
_DEFAULT_TEST_GROUP_NAME = "Sprint 2"


def _board_id_override() -> str:
    return (os.environ.get("PLAKY_BOARDMAN_TEST_BOARD_ID") or "").strip()


def _group_id_override() -> str:
    return (os.environ.get("PLAKY_BOARDMAN_TEST_GROUP_ID") or "").strip()


def _configured_group_name() -> str:
    return (
        os.environ.get("PLAKY_BOARDMAN_TEST_GROUP_NAME") or ""
    ).strip() or _DEFAULT_TEST_GROUP_NAME


def find_row_by_name(rows: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """First row whose name/title matches ``name`` (case-insensitive, stripped)."""
    key = name.strip().casefold()
    if not key:
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        n = str(row.get("name") or row.get("title") or "").strip().casefold()
        if n == key:
            return row
    return None


async def resolve_boardman_test_board_id(client: PlakyClient) -> str:
    override = _board_id_override()
    if override:
        return override
    r = await client.list_boards()
    if not r.get("ok"):
        raise AssertionError(
            f"list_boards failed: status={r.get('status')} message={r.get('message')!r} "
            "(set PLAKY_BOARDMAN_TEST_BOARD_ID to skip discovery)"
        )
    boards = r.get("boards") or []
    b = find_row_by_name(boards, BOARDMAN_TEST_BOARD_NAME)
    if not b or not b.get("id"):
        raise AssertionError(
            f"Board {BOARDMAN_TEST_BOARD_NAME!r} not found; create it or set PLAKY_BOARDMAN_TEST_BOARD_ID"
        )
    return str(b["id"])


async def resolve_boardman_test_group_id(client: PlakyClient, board_id: str) -> str:
    override = _group_id_override()
    if override:
        return override
    r = await client.list_groups(board_id)
    if not r.get("ok"):
        raise AssertionError(
            f"list_groups failed: status={r.get('status')} message={r.get('message')!r} "
            "(set PLAKY_BOARDMAN_TEST_GROUP_ID to skip discovery)"
        )
    groups = r.get("groups") or []
    gname = _configured_group_name()
    g = find_row_by_name(groups, gname)
    if not g or not g.get("id"):
        raise AssertionError(
            f"Group {gname!r} not found on board; create it or set PLAKY_BOARDMAN_TEST_GROUP_ID"
        )
    return str(g["id"])
