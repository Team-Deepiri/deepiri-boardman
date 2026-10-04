"""A fake in-memory ClickUp v2 API with real state, shared by the webhook-sync tests."""

from __future__ import annotations

import json
from typing import Any

import httpx

from boardman.clickup.client import ClickUpClient


class FakeClickUp:
    """Just enough of ClickUp's v2 API for the issue flow, with real state."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.log: list[tuple[str, str, dict]] = []
        self.fail_get = False
        self.fail_create = False
        self.deleted: list[str] = []
        self.n = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else {}
        path = req.url.path.removeprefix("/api/v2")
        self.log.append((req.method, path, body))
        parts = path.strip("/").split("/")
        if req.method == "GET" and parts[0] == "list" and parts[2] == "task":
            return httpx.Response(
                200, json={"tasks": list(self.tasks.values()), "last_page": True}
            )
        if req.method == "POST" and parts[0] == "list" and parts[2] == "task":
            if self.fail_create:
                return httpx.Response(500, text="boom")
            self.n += 1
            tid = f"t{self.n}"
            prio = body.get("priority")
            self.tasks[tid] = {
                "id": tid,
                "name": body["name"],
                "description": body.get("description", ""),
                "status": {"status": body.get("status") or "to do"},
                "priority": {"id": prio, "priority": str(prio)} if prio else None,
                "assignees": [{"id": a} for a in body.get("assignees", [])],
                "tags": [{"name": t} for t in body.get("tags", [])],
                "url": f"https://cu/{tid}",
            }
            return httpx.Response(200, json=self.tasks[tid])
        if parts[0] == "task":
            tid = parts[1]
            task = self.tasks.get(tid)
            if task is None:
                return httpx.Response(404, text="nope")
            if len(parts) == 2 and req.method == "DELETE":
                self.deleted.append(tid)
                self.tasks.pop(tid, None)
                return httpx.Response(204)
            if len(parts) == 2 and req.method == "GET":
                if self.fail_get:
                    return httpx.Response(500, text="down")
                return httpx.Response(200, json=task)
            if len(parts) == 2 and req.method == "PUT":
                if "name" in body:
                    task["name"] = body["name"]
                if "description" in body:
                    task["description"] = body["description"]
                if "archived" in body:
                    task["archived"] = body["archived"]
                if "status" in body:
                    task["status"] = {"status": body["status"]}
                if "priority" in body:
                    task["priority"] = {
                        "id": body["priority"],
                        "priority": str(body["priority"]),
                    }
                if "assignees" in body:
                    ids = {str(a["id"]) for a in task["assignees"]}
                    ids |= {str(i) for i in body["assignees"]["add"]}
                    ids -= {str(i) for i in body["assignees"]["rem"]}
                    task["assignees"] = [{"id": i} for i in sorted(ids)]
                return httpx.Response(200, json=task)
            if len(parts) == 4 and parts[2] == "field" and req.method == "POST":
                fields = task.setdefault("custom_fields", [])
                field = next((f for f in fields if f["id"] == parts[3]), None)
                if field is None:
                    field = {"id": parts[3], "value": []}
                    fields.append(field)
                cur = {str(u["id"]) for u in field["value"]}
                cur |= {str(i) for i in body["value"]["add"]}
                cur -= {str(i) for i in body["value"]["rem"]}
                field["value"] = [{"id": i} for i in sorted(cur)]
                return httpx.Response(200, json={})
            if len(parts) == 3 and parts[2] == "comment":
                task.setdefault("comments", []).append(body["comment_text"])
                return httpx.Response(200, json={"id": len(task["comments"])})
            if len(parts) == 4 and parts[2] == "tag":
                name = parts[3]
                if req.method == "POST":
                    if name not in [t["name"] for t in task["tags"]]:
                        task["tags"].append({"name": name})
                else:
                    task["tags"] = [t for t in task["tags"] if t["name"] != name]
                return httpx.Response(200, json={})
        return httpx.Response(404, text="unhandled")

    def client(self) -> ClickUpClient:
        return ClickUpClient(
            "tok", "https://cu.test/api/v2", transport=httpx.MockTransport(self.handler)
        )

    def writes(self, method: str) -> list[dict]:
        return [b for m, _, b in self.log if m == method]
