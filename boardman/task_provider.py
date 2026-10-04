"""Pick the task/board provider client for provider-neutral code paths.

``TASK_PROVIDER=plaky`` (default) keeps today's behavior. ``TASK_PROVIDER=clickup`` swaps in
``ClickUpClient``. Only the common task surface (create/get/list/update/comment/subtask) is
provider-neutral; board-schema and field-patching helpers remain Plaky-only.
"""

from __future__ import annotations

from typing import Any, Protocol

from boardman.settings import settings

PROVIDERS = ("plaky", "clickup")


class TaskClient(Protocol):
    """The task surface both providers implement. Anything beyond it is provider-specific."""

    async def get_tasks(self, status: str = "open", board_id: str | None = None) -> dict[str, Any]:
        ...

    async def get_task(self, task_id: str) -> dict[str, Any]:
        ...

    async def add_comment(
        self, task_id: str, body: str, *, board_id: str | None = None
    ) -> dict[str, Any]:
        ...

    async def create_task(
        self, title: str, description: str = "", priority: str = "medium", **kwargs: Any
    ) -> dict[str, Any]:
        ...


def active_provider() -> str:
    value = (settings.task_provider or "plaky").strip().lower()
    return value if value in PROVIDERS else "plaky"


def get_task_client() -> TaskClient:
    """Return a PlakyClient or ClickUpClient for the configured provider."""
    if active_provider() == "clickup":
        from boardman.clickup.client import ClickUpClient

        return ClickUpClient()
    from boardman.plaky.client import PlakyClient

    return PlakyClient()
