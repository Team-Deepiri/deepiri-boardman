"""Read-only check that the ClickUp setup can do its job, run once at startup.

A wrong status name is the quiet way ClickUp sync breaks: ClickUp answers ``400 Status does not
exist`` and the task simply stays where it was. This compares every ``CLICKUP_STATUS_*`` name
with the statuses of each list Boardman writes to, so a mistake shows up in the log when the
service starts instead of weeks later as a task that never moved.

It never writes to ClickUp and never raises: problems are returned for the caller to log.
"""

from __future__ import annotations

import logging
from typing import Any

from boardman.clickup.statuses import configured_statuses
from boardman.repos_config import list_registered_repos
from boardman.settings import settings

_log = logging.getLogger(__name__)


def configured_list_ids() -> list[str]:
    """Every list Boardman may write to: the default list plus each repo's ``clickup_list_id``."""
    ids: list[str] = []
    default = (settings.clickup_default_list_id or "").strip()
    if default:
        ids.append(default)
    try:
        routings = list_registered_repos().values()
    except Exception:  # noqa: BLE001 - an unreadable repos.yml is reported elsewhere
        routings = []
    for routing in routings:
        lid = (getattr(routing, "clickup_list_id", "") or "").strip()
        if lid and lid not in ids:
            ids.append(lid)
    return ids


async def clickup_config_problems(client: Any) -> list[str]:
    """Human-readable problems with the ClickUp configuration; empty when it looks right."""
    if not (settings.clickup_api_token or "").strip():
        return ["CLICKUP_API_TOKEN is not set: ClickUp sync cannot run."]
    lists = configured_list_ids()
    if not lists:
        return [
            "No ClickUp list is configured: set CLICKUP_DEFAULT_LIST_ID or add clickup_list_id "
            "to repos in repos.yml."
        ]
    problems: list[str] = []
    for list_id in lists:
        try:
            got = await client.get_list(list_id)
        except Exception as exc:  # noqa: BLE001 - this check must never take the service down
            problems.append(f"List {list_id}: could not be read ({exc}).")
            continue
        if not got.get("ok"):
            problems.append(
                f"List {list_id}: could not be read ({got.get('message') or 'unknown error'})."
            )
            continue
        present = {name.casefold() for name in got.get("statuses") or []}
        for intent, name in configured_statuses():
            if name.casefold() not in present:
                problems.append(
                    f"List {list_id} has no status {name!r} (used for {intent}): tasks will "
                    "stay where they are until it is added."
                )
    return problems


async def log_clickup_config_problems(client: Any) -> list[str]:
    """Run the check and log one WARNING per problem (or one INFO line when clean)."""
    problems = await clickup_config_problems(client)
    for problem in problems:
        _log.warning("ClickUp config: %s", problem)
    if not problems:
        _log.info("ClickUp config: token, lists and status names check out")
    return problems
