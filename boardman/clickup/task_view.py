"""Read helpers over a ClickUp task payload, and the one-`type:`-tag rule."""

from __future__ import annotations

from typing import Any

from boardman.clickup.client import ClickUpClient

TYPE_TAG_PREFIX = "type:"


def current_status(task: dict[str, Any]) -> str:
    status = task.get("status")
    return str(status.get("status") if isinstance(status, dict) else status or "").strip()


def current_assignees(task: dict[str, Any]) -> list[str]:
    return [str(a.get("id")) for a in task.get("assignees") or [] if isinstance(a, dict)]


def current_type_tags(task: dict[str, Any]) -> list[str]:
    names = [str(t.get("name") or "") for t in task.get("tags") or [] if isinstance(t, dict)]
    return [n for n in names if n.startswith(TYPE_TAG_PREFIX)]


def user_ids(user_id: str) -> list[int] | None:
    """ClickUp wants integer user ids; None when the value is not one."""
    text = str(user_id).strip()
    return [int(text)] if text.isdigit() else None


def type_tag(task_type: str) -> str:
    return f"{TYPE_TAG_PREFIX}{task_type.strip().lower()}" if (task_type or "").strip() else ""


async def sync_type_tag(
    client: ClickUpClient, task_id: str, task: dict[str, Any], task_type: str
) -> list[dict[str, Any]]:
    """Leave exactly one ``type:`` tag on the task: the wanted one. Returns the calls made."""
    wanted = type_tag(task_type)
    ops: list[dict[str, Any]] = []
    if not wanted:
        return ops
    have = current_type_tags(task)
    if wanted not in have:
        ops.append(await client.add_tag(task_id, wanted))
    for stale in have:
        if stale != wanted:
            ops.append(await client.remove_tag(task_id, stale))
    return ops
