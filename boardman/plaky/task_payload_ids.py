"""Extract board/group ids from Plaky task or public item JSON."""

from __future__ import annotations


def board_id_from_plaky_task(task: dict | None) -> str:
    if not isinstance(task, dict):
        return ""
    for k in ("boardId", "board_id"):
        val = task.get(k)
        if isinstance(val, dict):
            val = val.get("id") or val.get("boardId")
        if val is not None and str(val).strip():
            return str(val).strip()
    board = task.get("board")
    if isinstance(board, dict):
        val = board.get("id") or board.get("boardId")
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def placement_ids_from_plaky_task(task: dict | None) -> tuple[str, str]:
    """Board and group ids from a Plaky task or public item dict."""
    bid = board_id_from_plaky_task(task)
    gid = ""
    if isinstance(task, dict):
        for k in ("groupId", "group_id"):
            val = task.get(k)
            if isinstance(val, dict):
                val = val.get("id")
            if val is not None and str(val).strip():
                gid = str(val).strip()
                break
        if not gid:
            grp = task.get("group")
            if isinstance(grp, dict):
                v = grp.get("id") or grp.get("groupId")
                if v is not None and str(v).strip():
                    gid = str(v).strip()
    return bid, gid
