"""Blocking ClickUp client for code that cannot await (synchronous config loading).

Kept apart from ``ClickUpClient`` so the async client stays purely async. Never use this from
async code: every call stalls the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from boardman.clickup.client import ClickUpClient, retry_delay

_log = logging.getLogger(__name__)


class SyncClickUpClient(ClickUpClient):
    """``ClickUpClient`` configuration and parsing with a blocking transport. It exposes only
    the blocking calls below; do not call the inherited ``async`` methods on it."""

    def _request_sync(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, Any]] | dict[str, Any] | None = None,
        retries: int = 2,
    ) -> httpx.Response:
        """Blocking twin of ``ClickUpClient._request``; it shares ``retry_delay`` with it so the
        two cannot drift apart."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # no running loop: blocking here is fine
        else:
            _log.warning(
                "SyncClickUpClient blocking call made on a running event loop; this stalls it"
            )
        url = f"{self.base_url}{path}"
        transport = self._transport if isinstance(self._transport, httpx.BaseTransport) else None
        idempotent = method.upper() != "POST"
        response: httpx.Response | None = None
        with httpx.Client(transport=transport, timeout=self.timeout) as client:
            for attempt in range(retries + 1):
                try:
                    response = client.request(method, url, headers=self._headers(), params=params)
                except httpx.RequestError:
                    delay = retry_delay(None, "", attempt, retries, idempotent=idempotent)
                    if delay is None:
                        raise
                    time.sleep(delay)
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
                time.sleep(delay)
        assert response is not None
        return response

    def list_workspace_users_sync(self) -> dict[str, Any]:
        """Same result shape as ``ClickUpClient.list_workspace_users``."""
        if not self.api_token:
            return self._missing_token(users=[])
        response = self._request_sync("GET", "/team")
        if response.status_code != 200:
            return {**self._failure(response, "list workspaces"), "users": []}
        return {"ok": True, "status": 200, "users": self._users_from_teams(response.json())}
