import time
from typing import Any, Dict, List, Optional

import httpx

from boardman.settings import settings


async def _request_with_rate_limit_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: Dict[str, str],
    json: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    retries: int = 2,
) -> httpx.Response:
    for attempt in range(retries + 1):
        response = await client.request(
            method=method, url=url, headers=headers, json=json, params=params, timeout=20
        )

        if response.status_code != 429:
            return response

        if attempt == retries:
            return response

        retry_after = response.headers.get("Retry-After")
        wait_seconds = int(retry_after) if retry_after and retry_after.isdigit() else 2
        time.sleep(wait_seconds)

    return response


def _headers(api_key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"}


class PlakyClient:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or settings.plaky_api_key
        self.base_url = base_url or settings.plaky_api_base
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient()
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def create_task(
        self, title: str, description: str, priority: str = "medium"
    ) -> Dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing."}

        url = f"{self.base_url}/tasks"
        body = {"title": title, "description": description, "priority": priority}

        async with httpx.AsyncClient() as client:
            response = await _request_with_rate_limit_retry(
                client, "POST", url, headers=_headers(self.api_key), json=body
            )

        if response.status_code in (200, 201):
            payload = response.json()
            task_id = payload.get("id") or payload.get("taskId")
            task_url = (
                payload.get("url") or payload.get("taskUrl") or (f"https://app.plaky.com/task/{task_id}" if task_id else None)
            )
            return {"ok": True, "status": response.status_code, "task": payload, "task_url": task_url}

        if response.status_code == 429:
            return {"ok": False, "status": 429, "message": "Plaky API rate limited the request."}

        return {"ok": False, "status": response.status_code, "message": f"Failed to create task ({response.status_code}): {response.text[:200]}"}

    async def get_tasks(self, status: str = "open") -> Dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing."}

        url = f"{self.base_url}/tasks"
        params = {"status": status}

        async with httpx.AsyncClient() as client:
            response = await _request_with_rate_limit_retry(
                client, "GET", url, headers=_headers(self.api_key), params=params
            )

        if response.status_code == 200:
            payload = response.json()
            tasks: List[Dict[str, Any]] = payload if isinstance(payload, list) else payload.get("tasks", [])
            return {"ok": True, "status": response.status_code, "tasks": tasks}

        if response.status_code == 429:
            return {"ok": False, "status": 429, "message": "Plaky API rate limited the request."}

        return {"ok": False, "status": response.status_code, "message": f"Failed to fetch tasks ({response.status_code}): {response.text[:200]}"}

    async def add_comment(self, task_id: str, body: str) -> Dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing."}

        url = f"{self.base_url}/tasks/{task_id}/comments"
        payload = {"body": body}

        async with httpx.AsyncClient() as client:
            response = await _request_with_rate_limit_retry(
                client, "POST", url, headers=_headers(self.api_key), json=payload
            )

        if response.status_code in (200, 201):
            return {"ok": True, "status": response.status_code, "comment": response.json()}

        if response.status_code == 429:
            return {"ok": False, "status": 429, "message": "Plaky API rate limited the request."}

        return {"ok": False, "status": response.status_code, "message": f"Failed to add comment ({response.status_code}): {response.text[:200]}"}

    async def update_task_status(self, task_id: str, status: str) -> Dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing."}

        url = f"{self.base_url}/tasks/{task_id}"
        payload = {"status": status}

        async with httpx.AsyncClient() as client:
            response = await _request_with_rate_limit_retry(
                client, "PATCH", url, headers=_headers(self.api_key), json=payload
            )

        if response.status_code in (200, 201):
            return {"ok": True, "status": response.status_code, "task": response.json()}

        if response.status_code == 429:
            return {"ok": False, "status": 429, "message": "Plaky API rate limited the request."}

        return {"ok": False, "status": response.status_code, "message": f"Failed to update task ({response.status_code}): {response.text[:200]}"}

    async def get_task(self, task_id: str) -> Dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing."}

        url = f"{self.base_url}/tasks/{task_id}"

        async with httpx.AsyncClient() as client:
            response = await _request_with_rate_limit_retry(client, "GET", url, headers=_headers(self.api_key))

        if response.status_code == 200:
            return {"ok": True, "status": response.status_code, "task": response.json()}

        if response.status_code == 429:
            return {"ok": False, "status": 429, "message": "Plaky API rate limited the request."}

        return {"ok": False, "status": response.status_code, "message": f"Failed to get task ({response.status_code}): {response.text[:200]}"}