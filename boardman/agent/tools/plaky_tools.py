"""LangChain tools wrapping PlakyClient (async)."""

from __future__ import annotations

import json
from typing import List, Optional

from langchain_core.tools import StructuredTool

from boardman.plaky.client import PlakyClient


async def _plaky_list_tasks(status: str = "open") -> str:
    c = PlakyClient()
    r = await c.get_tasks(status=status)
    return json.dumps(r, default=str)[:12000]


async def _plaky_get_task(task_id: str) -> str:
    c = PlakyClient()
    r = await c.get_task(task_id)
    return json.dumps(r, default=str)[:12000]


async def _plaky_create_task(
    title: str, description: str, priority: str = "medium", repo_tag: str = ""
) -> str:
    c = PlakyClient()
    full = f"[{repo_tag}] {title}" if repo_tag else title
    r = await c.create_task(title=full, description=description, priority=priority)
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
                        "Create a Plaky item (task). Uses board/group from the user's UI selection when set; "
                        "otherwise env defaults or legacy /tasks API. Args: title, description, "
                        "priority (low|medium|high), optional repo_tag for [tag] prefix."
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
