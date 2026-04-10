"""Tool risk classes — mutating tools only when API/CLI allows writes."""

from __future__ import annotations

from typing import FrozenSet

# Tool names registered on the LangChain agent
READ_ONLY_TOOLS: FrozenSet[str] = frozenset(
    {
        "plaky_list_boards",
        "plaky_match_board",
        "plaky_match_group",
        "plaky_board_schema",
        "plaky_list_tasks",
        "plaky_get_task",
        "plaky_get_board_item",
        "plaky_list_workspace_users",
        "plaky_save_task_preferences",
        "scan_local_repo",
        "github_list_open_issues",
        "github_fetch_direction",
        "github_fetch_file",
        "github_repo_planning_context",
        "assignment_preview",
    }
)

WRITE_TOOLS: FrozenSet[str] = frozenset(
    {
        "plaky_create_task",
        "plaky_patch_item_fields",
        "plaky_update_task",
        "plaky_add_comment",
        "plaky_create_subtask",
    }
)


def is_write_tool(name: str) -> bool:
    return name in WRITE_TOOLS
