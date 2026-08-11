"""Resolve board/group IDs from repos.yml routing (Plaky: Board -> Group -> Item)."""

from __future__ import annotations

from boardman.repos_config import RepoRouting


def effective_plaky_placement(routing: RepoRouting | None) -> tuple[str | None, str | None]:
    """Return (board_id, group_id) for creates; None means use legacy /tasks without placement."""
    bid: str | None = None
    gid: str | None = None
    if routing:
        if routing.plaky_board_id.strip():
            bid = routing.plaky_board_id.strip()
        if routing.plaky_group_id.strip():
            gid = routing.plaky_group_id.strip()
    return bid, gid
