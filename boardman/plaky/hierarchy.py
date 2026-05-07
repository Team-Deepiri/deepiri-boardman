"""Resolve board/group IDs from repos.yml routing (Plaky: Board -> Group -> Item)."""

from __future__ import annotations

from typing import Optional, Tuple

from boardman.repos_config import RepoRouting


def effective_plaky_placement(routing: Optional[RepoRouting]) -> Tuple[Optional[str], Optional[str]]:
    """Return (board_id, group_id) for creates; None means use legacy /tasks without placement."""
    bid: Optional[str] = None
    gid: Optional[str] = None
    if routing:
        if routing.plaky_board_id.strip():
            bid = routing.plaky_board_id.strip()
        if routing.plaky_group_id.strip():
            gid = routing.plaky_group_id.strip()
    return bid, gid
