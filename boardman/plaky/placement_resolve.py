"""Resolve Plaky board/group ids using live API lists + fuzzy name ranking (see name_match.rank_plaky_rows)."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from boardman.plaky.client import PlakyClient
from boardman.plaky.name_match import rank_plaky_rows


async def match_boards(name_query: str) -> Dict[str, Any]:
    """List boards and rank by name; same shape as plaky_match_board tool output (without JSON string)."""
    c = PlakyClient()
    raw = await c.list_boards()
    boards = raw.get("boards") or []
    if not isinstance(boards, list):
        boards = []
    matches, best = rank_plaky_rows(boards, name_query)
    return {
        "list_ok": raw.get("ok"),
        "message": raw.get("message"),
        "matches": matches[:25],
        "best": best,
    }


async def match_groups(board_id: str, name_query: str) -> Dict[str, Any]:
    """List groups on a board and rank by name."""
    c = PlakyClient()
    raw = await c.list_groups(board_id.strip())
    groups = raw.get("groups") or []
    if not isinstance(groups, list):
        groups = []
    matches, best = rank_plaky_rows(groups, name_query)
    return {
        "list_ok": raw.get("ok"),
        "message": raw.get("message"),
        "matches": matches[:25],
        "best": best,
    }


async def resolve_board_id_from_query(name_query: str) -> Tuple[Optional[str], Dict[str, Any]]:
    bundle = await match_boards(name_query)
    best = bundle.get("best")
    if isinstance(best, dict) and str(best.get("id") or "").strip():
        return str(best["id"]).strip(), bundle
    return None, bundle


async def resolve_group_id_from_query(board_id: str, name_query: str) -> Tuple[Optional[str], Dict[str, Any]]:
    bundle = await match_groups(board_id, name_query)
    best = bundle.get("best")
    if isinstance(best, dict) and str(best.get("id") or "").strip():
        return str(best["id"]).strip(), bundle
    return None, bundle


async def resolve_scan_placement(
    *,
    board_id: str = "",
    group_id: str = "",
    group_fallback_name: str = "",
    board_name_query: str = "",
    group_name_query: str = "",
) -> Tuple[str, str, Dict[str, Any]]:
    """
    Fill missing board/group ids using optional fuzzy name queries.

    ``group_fallback_name`` is typically ``repos.yml`` ``plaky_table`` (section label in UI).
    Auto-match only applies when ``rank_plaky_rows`` returns a strong ``best`` (score >= 400).
    """
    meta: Dict[str, Any] = {}
    bid = (board_id or "").strip()
    gid = (group_id or "").strip()
    bq = (board_name_query or "").strip()
    gq = (group_name_query or "").strip()
    fallback = (group_fallback_name or "").strip()

    if not bid and bq:
        resolved, info = await resolve_board_id_from_query(bq)
        meta["board_match"] = info
        if resolved:
            bid = resolved
        else:
            meta["board_error"] = "No strong board name match (try a clearer plaky_board_query)."
            return "", "", meta

    if bid and not gid:
        use_gq = gq or fallback
        if use_gq:
            resolved, info = await resolve_group_id_from_query(bid, use_gq)
            meta["group_match"] = info
            if resolved:
                gid = resolved
            else:
                meta["group_error"] = "No strong group name match for this board (try plaky_group_query)."

    return bid, gid, meta
