"""Tool risk classes — mutating tools only when API/CLI allows writes."""

from __future__ import annotations

from typing import FrozenSet

# Tool names registered on the LangChain agent
READ_ONLY_TOOLS: FrozenSet[str] = frozenset(
    {
        "plaky_list_tasks",
        "plaky_get_task",
        "scan_local_repo",
        "github_list_open_issues",
    }
)

WRITE_TOOLS: FrozenSet[str] = frozenset(
    {
        "plaky_create_task",
        "plaky_update_task",
        "plaky_add_comment",
        "plaky_create_subtask",
    }
)


def is_write_tool(name: str) -> bool:
    return name in WRITE_TOOLS
