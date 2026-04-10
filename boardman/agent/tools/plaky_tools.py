"""LangChain tools wrapping PlakyClient (async)."""

from __future__ import annotations

import json
from typing import List, Optional

from langchain_core.tools import StructuredTool

from boardman.plaky.board_schema import fetch_board_schema_bundle
from boardman.plaky.client import PlakyClient
from boardman.plaky.name_match import rank_plaky_rows


async def _plaky_list_tasks(status: str = "open") -> str:
    c = PlakyClient()
    r = await c.get_tasks(status=status)
    return json.dumps(r, default=str)[:12000]


async def _plaky_get_task(task_id: str) -> str:
    c = PlakyClient()
    r = await c.get_task(task_id)
    return json.dumps(r, default=str)[:12000]


async def _plaky_match_board(name_query: str) -> str:
    """List boards from Plaky API and rank by name vs `name_query` (e.g. user's board mention)."""
    c = PlakyClient()
    raw = await c.list_boards()
    boards = raw.get("boards") or []
    if not isinstance(boards, list):
        boards = []
    matches, best = rank_plaky_rows(boards, name_query)
    return json.dumps(
        {"list_ok": raw.get("ok"), "message": raw.get("message"), "matches": matches[:25], "best": best},
        default=str,
    )[:12000]


async def _plaky_board_schema(board_id: str) -> str:
    """Return groups + field definitions (status/type/priority options) for a board from the Plaky API."""
    bundle = await fetch_board_schema_bundle(board_id)
    out = {
        "ok": bundle.get("ok"),
        "message": bundle.get("message"),
        "board_fetch_ok": bundle.get("board_fetch_ok"),
        "groups_fetch_ok": bundle.get("groups_fetch_ok"),
        "normalized": bundle.get("normalized"),
        "markdown": bundle.get("markdown"),
    }
    return json.dumps(out, default=str)[:12000]


async def _plaky_match_group(board_id: str, name_query: str) -> str:
    """List groups on `board_id` and rank by name vs `name_query`."""
    c = PlakyClient()
    raw = await c.list_groups(board_id)
    groups = raw.get("groups") or []
    if not isinstance(groups, list):
        groups = []
    matches, best = rank_plaky_rows(groups, name_query)
    return json.dumps(
        {"list_ok": raw.get("ok"), "message": raw.get("message"), "matches": matches[:25], "best": best},
        default=str,
    )[:12000]


async def _plaky_create_task(
    title: str,
    description: str,
    priority: str = "medium",
    repo_tag: str = "",
    board_id: str = "",
    group_id: str = "",
) -> str:
    c = PlakyClient()
    full = f"[{repo_tag}] {title}" if repo_tag else title
    bid = board_id.strip() or None
    gid = group_id.strip() or None
    r = await c.create_task(
        title=full, description=description, priority=priority, board_id=bid, group_id=gid
    )
    return json.dumps(r, default=str)


async def _plaky_update_task(
    task_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    priority: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
    c = PlakyClient()
    r = await c.update_task_fields(
        task_id, title=title, description=description, priority=priority, status=status
    )
    return json.dumps(r, default=str)


async def _plaky_add_comment(task_id: str, body: str) -> str:
    c = PlakyClient()
    r = await c.add_comment(task_id, body)
    return json.dumps(r, default=str)


async def _plaky_create_subtask(parent_task_id: str, title: str, description: str = "") -> str:
    c = PlakyClient()
    r = await c.create_subtask(parent_task_id, title, description)
    return json.dumps(r, default=str)


def build_plaky_tools(*, allow_writes: bool) -> List[StructuredTool]:
    tools: List[StructuredTool] = [
        StructuredTool.from_function(
            coroutine=_plaky_match_board,
            name="plaky_match_board",
            description=(
                "Find a Plaky board by name. Calls the Plaky API to list boards, then matches "
                "`name_query` to board names (e.g. user said 'Deepiri Main board'). "
                "Returns `best` with id when confident; otherwise pick from `matches` by score. "
                "Args: name_query."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_plaky_match_group,
            name="plaky_match_group",
            description=(
                "Find a group (section) on a board by name. Args: board_id, name_query "
                "(e.g. 'Backlog', 'AI Bugs'). Use after plaky_match_board."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_plaky_board_schema,
            name="plaky_board_schema",
            description=(
                "Load this board's groups and field definitions from Plaky (status/type/priority "
                "allowed values when the API returns them). Args: board_id. "
                "Use after plaky_match_board when the user changes boards or schema is missing from context."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_plaky_list_tasks,
            name="plaky_list_tasks",
            description="List Plaky tasks. Args: status (open|done|... default open).",
        ),
        StructuredTool.from_function(
            coroutine=_plaky_get_task,
            name="plaky_get_task",
            description="Get one Plaky task by id. Args: task_id.",
        ),
    ]
    if allow_writes:
        tools.extend(
            [
                StructuredTool.from_function(
                    coroutine=_plaky_create_task,
                    name="plaky_create_task",
                    description=(
                        "Create a Plaky item (task). Prefer passing board_id and group_id from "
                        "plaky_match_board / plaky_match_group when the user named a board or column. "
                        "If omitted, uses UI session selection, then env defaults, else legacy /tasks API. "
                        "Args: title, description, priority (low|medium|high), optional repo_tag, "
                        "optional board_id, optional group_id."
                    ),
                ),
                StructuredTool.from_function(
                    coroutine=_plaky_update_task,
                    name="plaky_update_task",
                    description=(
                        "Patch a Plaky task. Args: task_id, optional title, description, priority, status."
                    ),
                ),
                StructuredTool.from_function(
                    coroutine=_plaky_add_comment,
                    name="plaky_add_comment",
                    description="Add a comment to a Plaky task. Args: task_id, body (markdown).",
                ),
                StructuredTool.from_function(
                    coroutine=_plaky_create_subtask,
                    name="plaky_create_subtask",
                    description=(
                        "Create a subtask or subtask comment on parent_task_id. "
                        "Args: parent_task_id, title, description."
                    ),
                ),
            ]
        )
    return tools
