"""ClickUp API v2 client.

Mirrors the task-level surface of ``PlakyClient`` (same method names, same ``{"ok", "status", ...}``
result envelopes) so the generic task routes can run against either provider. It deliberately does
not mirror Plaky's board-schema and field-patching helpers: ClickUp has no equivalent, so code that
needs those stays Plaky-only.

ClickUp vocabulary: a Plaky *board* maps to a ClickUp *list* (``board_id`` is a list id here),
and ``priority`` is an integer, 1 (urgent) to 4 (low).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from boardman.github.http import shared_clickup_client
from boardman.settings import settings

_log = logging.getLogger(__name__)

_PRIORITY = {"urgent": 1, "critical": 1, "high": 2, "medium": 3, "normal": 3, "low": 4}
_PRIORITY_LABEL = {1: "urgent", 2: "high", 3: "normal", 4: "low"}
_TRANSIENT = frozenset({500, 502, 503, 504})


def retry_delay(
    status: int | None,
    retry_after: str,
    attempt: int,
    retries: int,
    *,
    idempotent: bool,
) -> float | None:
    """Seconds to wait before retrying, or None to stop.

    ``status`` is None for a network error. Shared by the async and blocking request loops so
    the retry rules cannot drift apart. POST is never retried on a network error or a 5xx, so a
    blip cannot double-create a task; a 429 is always safe to retry.
    """
    if attempt >= retries:
        return None
    if status == 429:
        return float(min(int(retry_after), 30)) if retry_after.isdigit() else 2.0
    if (status is None or status in _TRANSIENT) and idempotent:
        return 0.5 * (2**attempt)
    return None


def clickup_priority(value: str | int | None) -> int | None:
    """Map Boardman/Plaky priority names (or 1-4) to ClickUp's integer priority."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 4 else None
    text = str(value).strip().lower()
    if text.isdigit():
        return clickup_priority(int(text))
    return _PRIORITY.get(text)


def clickup_priority_label(value: int | None) -> str | None:
    return _PRIORITY_LABEL.get(value) if value else None


class ClickUpClient:
    def __init__(
        self,
        api_token: str | None = None,
        base_url: str | None = None,
        *,
        default_list_id: str | None = None,
        team_id: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
    ):
        self.api_token = api_token if api_token is not None else settings.clickup_api_token
        self.base_url = (base_url or settings.clickup_api_base).rstrip("/")
        self.default_list_id = (
            default_list_id if default_list_id is not None else settings.clickup_default_list_id
        )
        self.team_id = team_id if team_id is not None else settings.clickup_team_id
        self._transport = transport
        self.timeout = timeout if timeout is not None else settings.clickup_api_timeout

    # -- plumbing ---------------------------------------------------------------------------

    def _missing_token(self, **empty: Any) -> dict[str, Any]:
        """The one error shape for a missing token. ``empty`` adds empty collections (such as
        ``users=[]``) so list-returning methods keep their usual keys."""
        return {"ok": False, "status": 400, "message": "CLICKUP_API_TOKEN is missing.", **empty}

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self.api_token, "Content-Type": "application/json"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: list[tuple[str, Any]] | dict[str, Any] | None = None,
        retries: int = 2,
    ) -> httpx.Response:
        """One request with 429 and transient-5xx retry. POST is never retried on 5xx, so a
        blip cannot double-create a task."""
        idempotent = method.upper() != "POST"
        url = f"{self.base_url}{path}"
        response: httpx.Response | None = None
        async with self._http() as client:
            for attempt in range(retries + 1):
                try:
                    response = await client.request(
                        method,
                        url,
                        headers=self._headers(),
                        json=json,
                        params=params,
                        timeout=self.timeout,
                    )
                except httpx.RequestError:
                    delay = retry_delay(None, "", attempt, retries, idempotent=idempotent)
                    if delay is None:
                        raise
                    await asyncio.sleep(delay)
                    continue
                delay = retry_delay(
                    response.status_code,
                    response.headers.get("Retry-After") or "",
                    attempt,
                    retries,
                    idempotent=idempotent,
                )
                if delay is None:
                    return response
                await asyncio.sleep(delay)
        assert response is not None
        return response

    @asynccontextmanager
    async def _http(self) -> AsyncIterator[httpx.AsyncClient]:
        """The pooled per-loop client; a test transport gets a throwaway client instead."""
        if self._transport is not None:
            async with httpx.AsyncClient(transport=self._transport) as client:
                yield client
        else:
            async with shared_clickup_client() as client:
                yield client

    @staticmethod
    def _failure(response: httpx.Response, what: str) -> dict[str, Any]:
        if response.status_code == 429:
            return {"ok": False, "status": 429, "message": "ClickUp API rate limited the request."}
        return {
            "ok": False,
            "status": response.status_code,
            "message": f"Failed to {what} ({response.status_code}): {response.text[:200]}",
        }

    # -- tasks ------------------------------------------------------------------------------

    async def create_task(
        self,
        title: str,
        description: str = "",
        priority: str | int | None = "medium",
        *,
        board_id: str | None = None,
        group_id: str | None = None,
        status: str | None = None,
        assignee_ids: list[int] | None = None,
        parent_task_id: str | None = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        """Create a task in a list (``board_id``, falling back to CLICKUP_DEFAULT_LIST_ID).

        ``group_id`` and Plaky-only keyword arguments (``field_values`` and friends) are accepted
        and ignored so call sites can stay provider-neutral.
        """
        if not self.api_token:
            return self._missing_token()
        list_id = (board_id or "").strip() or (self.default_list_id or "").strip()
        if not list_id:
            return {
                "ok": False,
                "status": 400,
                "message": "A ClickUp list id is required (board_id or CLICKUP_DEFAULT_LIST_ID).",
            }
        body: dict[str, Any] = {"name": title, "description": description or ""}
        prio = clickup_priority(priority)
        if prio:
            body["priority"] = prio
        if status:
            body["status"] = status
        if assignee_ids:
            body["assignees"] = assignee_ids
        if parent_task_id:
            body["parent"] = parent_task_id

        response = await self._request("POST", f"/list/{list_id}/task", json=body)
        if response.status_code in (200, 201):
            task = response.json()
            return {
                "ok": True,
                "status": response.status_code,
                "task": task,
                "task_id": str(task.get("id") or ""),
                "task_url": task.get("url"),
            }
        return self._failure(response, "create task")

    async def create_subtask(
        self,
        parent_task_id: str,
        title: str,
        description: str = "",
        *,
        status: str | None = None,
        priority: str | int | None = None,
        board_id: str | None = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        """A ClickUp subtask is a task with ``parent`` set. It must live in the parent's list, so
        look the list up when the caller does not pass it."""
        if not self.api_token:
            return self._missing_token()
        list_id = (board_id or "").strip()
        if not list_id:
            parent = await self.get_task(parent_task_id)
            if not parent.get("ok"):
                return parent
            list_id = str((parent["task"].get("list") or {}).get("id") or "")
        return await self.create_task(
            title,
            description,
            priority,
            board_id=list_id or None,
            status=status,
            parent_task_id=parent_task_id,
        )

    async def get_task(self, task_id: str) -> dict[str, Any]:
        if not self.api_token:
            return self._missing_token()
        response = await self._request("GET", f"/task/{task_id}")
        if response.status_code == 200:
            return {"ok": True, "status": 200, "task": response.json()}
        return self._failure(response, "get task")

    async def get_tasks(self, status: str = "open", board_id: str | None = None) -> dict[str, Any]:
        """List tasks in a list. ``"open"`` means not closed, ``"all"`` includes closed, and any
        other value filters by that ClickUp status name."""
        if not self.api_token:
            return self._missing_token()
        list_id = (board_id or "").strip() or (self.default_list_id or "").strip()
        if not list_id:
            return {
                "ok": False,
                "status": 400,
                "message": "A ClickUp list id is required (board_id or CLICKUP_DEFAULT_LIST_ID).",
            }
        wanted = (status or "open").strip().lower()
        base_params: list[tuple[str, Any]] = [
            ("include_closed", "true" if wanted == "all" else "false"),
            ("subtasks", "true"),
        ]
        if wanted not in ("open", "all", ""):
            base_params.append(("statuses[]", status))

        page_cap = max(1, settings.clickup_max_list_pages)
        tasks: list[dict[str, Any]] = []
        truncated = False
        for page in range(page_cap):
            response = await self._request(
                "GET", f"/list/{list_id}/task", params=[*base_params, ("page", page)]
            )
            if response.status_code != 200:
                return self._failure(response, "fetch tasks")
            payload = response.json()
            rows = payload.get("tasks") or []
            for row in rows:
                if isinstance(row, dict):
                    row["status_name"] = str((row.get("status") or {}).get("status") or "")
                    tasks.append(row)
            if payload.get("last_page", len(rows) < 100) or not rows:
                break
        else:
            truncated = True
            _log.warning(
                "ClickUp list %s has more than %d pages of tasks; results are truncated at %d",
                list_id,
                page_cap,
                len(tasks),
            )
        result: dict[str, Any] = {"ok": True, "status": 200, "tasks": tasks, "truncated": truncated}
        if truncated:
            result[
                "message"
            ] = f"List has more than {page_cap * 100} tasks; only the first {len(tasks)} were loaded."
        return result

    async def update_task_fields(
        self,
        task_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        priority: str | int | None = None,
        status: str | None = None,
        add_assignee_ids: list[int] | None = None,
        remove_assignee_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        if not self.api_token:
            return self._missing_token()
        body: dict[str, Any] = {}
        if add_assignee_ids or remove_assignee_ids:
            body["assignees"] = {
                "add": list(add_assignee_ids or []),
                "rem": list(remove_assignee_ids or []),
            }
        if title is not None:
            body["name"] = title
        if description is not None:
            body["description"] = description
        prio = clickup_priority(priority)
        if prio:
            body["priority"] = prio
        if status is not None:
            body["status"] = status
        if not body:
            return {"ok": False, "status": 400, "message": "No fields to update."}
        response = await self._request("PUT", f"/task/{task_id}", json=body)
        if response.status_code in (200, 201):
            return {"ok": True, "status": response.status_code, "task": response.json()}
        return self._failure(response, "update task")

    async def add_comment(
        self, task_id: str, body: str, *, board_id: str | None = None
    ) -> dict[str, Any]:
        if not self.api_token:
            return self._missing_token()
        tid = (task_id or "").strip()
        if not tid:
            return {"ok": False, "status": 400, "message": "task_id is required."}
        response = await self._request(
            "POST", f"/task/{tid}/comment", json={"comment_text": body or ""}
        )
        if response.status_code in (200, 201):
            return {"ok": True, "status": response.status_code, "comment": response.json()}
        return self._failure(response, "add comment")

    # -- workspace --------------------------------------------------------------------------

    async def list_workspace_users(self) -> dict[str, Any]:
        """Members of the configured workspace (CLICKUP_TEAM_ID, else the first workspace)."""
        if not self.api_token:
            return self._missing_token(users=[])
        response = await self._request("GET", "/team")
        if response.status_code != 200:
            return {**self._failure(response, "list workspaces"), "users": []}
        teams = response.json().get("teams") or []
        team = next((t for t in teams if str(t.get("id")) == str(self.team_id)), None)
        team = team or (teams[0] if teams else {})
        users: list[dict[str, Any]] = []
        for member in team.get("members") or []:
            user = member.get("user") if isinstance(member, dict) else None
            if not isinstance(user, dict) or user.get("id") is None:
                continue
            users.append(
                {
                    "id": str(user["id"]),
                    "name": str(user.get("username") or user.get("email") or user["id"]),
                    "email": user.get("email"),
                    "github_login": None,
                }
            )
        return {"ok": True, "status": 200, "users": users}

    async def list_boards(self) -> dict[str, Any]:
        """All lists (the ClickUp equivalent of boards), across spaces, folders and folderless."""
        if not self.api_token:
            return self._missing_token(boards=[])
        team_id = (self.team_id or "").strip()
        if not team_id:
            response = await self._request("GET", "/team")
            if response.status_code != 200:
                return {**self._failure(response, "list workspaces"), "boards": []}
            teams = response.json().get("teams") or []
            team_id = str(teams[0]["id"]) if teams else ""
        if not team_id:
            return {"ok": True, "status": 200, "boards": []}

        spaces = await self._request("GET", f"/team/{team_id}/space", params={"archived": "false"})
        if spaces.status_code != 200:
            return {**self._failure(spaces, "list spaces"), "boards": []}
        boards: list[dict[str, str]] = []

        async def _lists(path: str) -> list[dict[str, Any]]:
            r = await self._request("GET", path, params={"archived": "false"})
            return (r.json().get("lists") or []) if r.status_code == 200 else []

        for space in spaces.json().get("spaces") or []:
            sid = str(space.get("id"))
            found = await _lists(f"/space/{sid}/list")
            folders = await self._request(
                "GET", f"/space/{sid}/folder", params={"archived": "false"}
            )
            if folders.status_code == 200:
                for folder in folders.json().get("folders") or []:
                    found.extend(
                        folder.get("lists") or await _lists(f"/folder/{folder['id']}/list")
                    )
            for item in found:
                boards.append(
                    {"id": str(item.get("id")), "name": str(item.get("name") or ""), "space": sid}
                )
        return {"ok": True, "status": 200, "boards": boards}
