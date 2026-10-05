"""Planning context from ClickUp lists. The ClickUp counterpart of ``PlakyPlanningContext``.

The team-to-board mapping file (``PLANNING_TEAM_PLAKY_BOARDS_FILE``) is reused: with
``TASK_PROVIDER=clickup`` the ids in it are ClickUp list ids.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from boardman.clickup.client import ClickUpClient
from boardman.planning.huddle.context_plaky import PlakyPlanningContext
from boardman.planning.huddle.team_plaky_boards import PlakyBoardRef
from boardman.settings import settings


def _iso_from_ms(value: object) -> str:
    """ClickUp stamps times as epoch milliseconds in a string."""
    text = str(value or "").strip()
    if not text.isdigit():
        return ""
    return datetime.fromtimestamp(int(text) / 1000, UTC).isoformat()


def normalize_task(task: dict[str, Any]) -> dict[str, Any]:
    """A ClickUp task in the shape the shared summary helpers read."""
    status = task.get("status_name") or (task.get("status") or {}).get("status") or ""
    people = [
        str(a.get("username") or a.get("email") or "")
        for a in task.get("assignees") or []
        if isinstance(a, dict)
    ]
    return {
        "id": task.get("id"),
        "title": task.get("name"),
        "status": str(status),
        "assignees": [p for p in people if p],
        "updatedAt": _iso_from_ms(task.get("date_updated")),
    }


class ClickUpPlanningContext(PlakyPlanningContext):
    provider_label = "ClickUp"
    unit_label = "list"
    unit_label_plural = "lists"
    key_hint = "CLICKUP_API_TOKEN"

    def enabled(self) -> bool:
        return bool(settings.clickup_api_token)

    async def _list_items(self, board: PlakyBoardRef) -> list[dict[str, Any]]:
        result = await ClickUpClient().get_tasks(status="all", board_id=board.board_id)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("message") or "get_tasks failed"))
        return [normalize_task(t) for t in result.get("tasks") or [] if isinstance(t, dict)]
