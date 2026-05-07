"""Tool risk classes — mutating tools only when API/CLI allows writes."""

from __future__ import annotations

import re
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
        "plaky_review_board",
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


_ORGANIZE_PATTERNS = (
    r"\b(re)?organi[sz]e\b",
    r"\breorder\b",
    r"\bcleanup\b",
    r"\bclean up\b",
    r"\bbulk\s+(update|move|archive|close|delete|merge)\b",
    r"\bmove\b.+\btask",
    r"\barchive\s+(the|all|every|these|my|our)\b",
)
_CONFIRM_PATTERN = re.compile(
    r"\b("
    r"confirm(ed)?|"
    r"approve(d)?|"
    r"apply(\s+(now|changes))?|"
    r"go\s+ahead|"
    r"do\s+it|"
    r"yes,?\s*(apply|go|do\s+it|please)"
    r")\b",
    re.IGNORECASE,
)


def looks_like_board_organize_request(message: str) -> bool:
    m = (message or "").strip().lower()
    if not m:
        return False
    return any(re.search(p, m, re.IGNORECASE) is not None for p in _ORGANIZE_PATTERNS)


def has_confirm_token(message: str) -> bool:
    return _CONFIRM_PATTERN.search((message or "").strip()) is not None
