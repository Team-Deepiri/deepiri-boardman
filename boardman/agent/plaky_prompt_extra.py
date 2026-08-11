"""System-prompt snippets for Plaky board/group context (UI + env)."""

from __future__ import annotations


def plaky_placement_markdown(board_id: str | None, group_id: str | None) -> str:
    """Tell the model which board/group to use so it does not re-ask the user."""
    bid = (board_id or "").strip() or None
    gid = (group_id or "").strip() or None
    if not bid and not gid:
        return ""
    lines = [
        "",
        "## Current Plaky placement (UI session + server env defaults)",
        "",
        "These ids are **already selected**. Use them for **plaky_create_task** (pass `board_id` and `group_id` explicitly). "
        "**Do not** ask the user which board or group to use when the relevant id(s) appear below.",
        "",
    ]
    if bid:
        lines.append(f"- **board_id**: `{bid}`")
    else:
        lines.append(
            "- **board_id**: not set — use **plaky_list_boards** / **plaky_match_board** if you need one."
        )
    if gid:
        lines.append(f"- **group_id** (section): `{gid}`")
    else:
        lines.append(
            "- **group_id**: not set — use **plaky_match_group** with the board_id above, "
            "or pick a group from the board schema block."
        )
    lines.append("")
    return "\n".join(lines)
