"""Plaky hierarchy: boards and groups (for UI + API discovery)."""

from __future__ import annotations

from fastapi import APIRouter

from boardman.plaky.client import PlakyClient

router = APIRouter()


@router.get("/plaky/boards")
async def plaky_boards() -> dict:
    c = PlakyClient()
    r = await c.list_boards()
    return {"ok": r.get("ok"), "boards": r.get("boards", []), "message": r.get("message")}


@router.get("/plaky/boards/{board_id}/groups")
async def plaky_board_groups(board_id: str) -> dict:
    c = PlakyClient()
    r = await c.list_groups(board_id)
    return {"ok": r.get("ok"), "groups": r.get("groups", []), "message": r.get("message")}
