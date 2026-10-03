"""Pick the task/board provider client for provider-neutral code paths.

``TASK_PROVIDER=plaky`` (default) keeps today's behavior. ``TASK_PROVIDER=clickup`` swaps in
``ClickUpClient``. Only the common task surface (create/get/list/update/comment/subtask) is
provider-neutral; board-schema and field-patching helpers remain Plaky-only.
"""

from __future__ import annotations

from typing import Any

from boardman.settings import settings

PROVIDERS = ("plaky", "clickup")


def active_provider() -> str:
    value = (settings.task_provider or "plaky").strip().lower()
    return value if value in PROVIDERS else "plaky"


def get_task_client() -> Any:
    """Return a PlakyClient or ClickUpClient for the configured provider."""
    if active_provider() == "clickup":
        from boardman.clickup.client import ClickUpClient

        return ClickUpClient()
    from boardman.plaky.client import PlakyClient

    return PlakyClient()
