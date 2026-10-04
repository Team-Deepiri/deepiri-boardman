"""ClickUp client: request shapes against a mocked transport. No network, no token needed."""

from __future__ import annotations

import json

import httpx
import pytest

from boardman.clickup.client import ClickUpClient, clickup_priority


def _client(handler, **kw) -> ClickUpClient:
    kw.setdefault("default_list_id", "L1")
    return ClickUpClient(
        "tok_123", "https://cu.test/api/v2", transport=httpx.MockTransport(handler), **kw
    )


def test_priority_mapping():
    assert clickup_priority("urgent") == 1
    assert clickup_priority("High") == 2
    assert clickup_priority("medium") == 3
    assert clickup_priority("low") == 4
    assert clickup_priority(2) == 2
    assert clickup_priority("3") == 3
    assert clickup_priority("nonsense") is None
    assert clickup_priority(None) is None
    assert clickup_priority(9) is None


async def test_missing_token_fails_cleanly():
    r = await ClickUpClient("", "https://cu.test").create_task("x", "y")
    assert r["ok"] is False and r["status"] == 400 and "CLICKUP_API_TOKEN" in r["message"]


async def test_create_task_sends_token_unprefixed_and_maps_fields():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers["authorization"]
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"id": "abc", "url": "https://app.clickup.com/t/abc"})

    r = await _client(handler).create_task("Fix bug", "details", "high", assignee_ids=[7])
    assert seen["auth"] == "tok_123"  # personal tokens are not "Bearer" prefixed
    assert seen["url"] == "https://cu.test/api/v2/list/L1/task"
    assert seen["body"] == {
        "name": "Fix bug",
        "description": "details",
        "priority": 2,
        "assignees": [7],
    }
    assert r["ok"] and r["task_id"] == "abc" and r["task_url"] == "https://app.clickup.com/t/abc"


async def test_create_task_needs_a_list():
    r = await _client(lambda req: httpx.Response(500), default_list_id="").create_task("x")
    assert r["ok"] is False and r["status"] == 400 and "list id" in r["message"]


async def test_create_task_ignores_plaky_only_kwargs():
    def handler(req):
        return httpx.Response(200, json={"id": "1"})

    r = await _client(handler).create_task(
        "x", field_values={"a": 1}, person_field_keys={"a"}, defer_field_patch=True
    )
    assert r["ok"]


async def test_create_task_is_not_retried_on_5xx():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503, text="down")

    r = await _client(handler).create_task("x")
    assert r["ok"] is False and r["status"] == 503
    assert len(calls) == 1  # a retried POST could create a duplicate


async def test_get_is_retried_on_5xx_then_succeeds(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("boardman.clickup.client.asyncio.sleep", no_sleep)
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json={"id": "t1"})

    r = await _client(handler).get_task("t1")
    assert r["ok"] and r["task"]["id"] == "t1" and len(calls) == 2


async def test_rate_limit_returns_a_clear_message(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("boardman.clickup.client.asyncio.sleep", no_sleep)
    r = await _client(lambda req: httpx.Response(429)).get_task("t1")
    assert r == {"ok": False, "status": 429, "message": "ClickUp API rate limited the request."}


async def test_get_tasks_open_excludes_closed_and_paginates():
    pages = []

    def handler(req: httpx.Request) -> httpx.Response:
        q = dict(req.url.params.multi_items())
        pages.append(q)
        assert q["include_closed"] == "false"
        n = int(q["page"])
        rows = [
            {"id": f"t{n}-{i}", "status": {"status": "to do"}} for i in range(100 if n == 0 else 3)
        ]
        return httpx.Response(200, json={"tasks": rows, "last_page": n == 1})

    r = await _client(handler).get_tasks("open")
    assert r["ok"] and len(r["tasks"]) == 103
    assert r["tasks"][0]["status_name"] == "to do"
    assert [p["page"] for p in pages] == ["0", "1"]


async def test_get_tasks_status_filter_and_all():
    got = []

    def handler(req):
        got.append(dict(req.url.params.multi_items()))
        return httpx.Response(200, json={"tasks": [], "last_page": True})

    await _client(handler).get_tasks("in review")
    await _client(handler).get_tasks("all")
    assert got[0]["statuses[]"] == "in review" and got[0]["include_closed"] == "false"
    assert got[1]["include_closed"] == "true" and "statuses[]" not in got[1]


async def test_update_task_fields_uses_put_and_rejects_empty():
    seen = {}

    def handler(req):
        seen["method"], seen["body"] = req.method, json.loads(req.content)
        return httpx.Response(200, json={"id": "t1"})

    c = _client(handler)
    assert (await c.update_task_fields("t1"))["status"] == 400
    r = await c.update_task_fields("t1", title="New", priority="urgent", status="done")
    assert r["ok"] and seen["method"] == "PUT"
    assert seen["body"] == {"name": "New", "priority": 1, "status": "done"}


async def test_add_comment_posts_comment_text():
    seen = {}

    def handler(req):
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, json={"id": 55})

    r = await _client(handler).add_comment("t1", "PR linked")
    assert (
        r["ok"]
        and seen["url"].endswith("/task/t1/comment")
        and seen["body"] == {"comment_text": "PR linked"}
    )
    assert (await _client(handler).add_comment("", "x"))["status"] == 400


async def test_create_subtask_finds_parents_list():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.method == "GET":
            return httpx.Response(200, json={"id": "p1", "list": {"id": "LP"}})
        assert json.loads(req.content)["parent"] == "p1"
        return httpx.Response(200, json={"id": "s1"})

    r = await _client(handler, default_list_id="").create_subtask("p1", "child")
    assert r["ok"] and r["task_id"] == "s1"
    assert calls[1] == ("POST", "/api/v2/list/LP/task")


async def test_list_workspace_users_prefers_configured_team():
    teams = {
        "teams": [
            {"id": "1", "members": [{"user": {"id": 10, "username": "ann", "email": "a@x.io"}}]},
            {"id": "2", "members": [{"user": {"id": 20, "username": "bob"}}, {"user": {}}]},
        ]
    }
    r = await _client(
        lambda req: httpx.Response(200, json=teams), team_id="2"
    ).list_workspace_users()
    assert r["ok"] and r["users"] == [
        {"id": "20", "name": "bob", "email": None, "github_login": None}
    ]


async def test_list_boards_collects_folder_and_folderless_lists():
    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p.endswith("/team/T/space"):
            return httpx.Response(200, json={"spaces": [{"id": "S"}]})
        if p.endswith("/space/S/list"):
            return httpx.Response(200, json={"lists": [{"id": "L1", "name": "Backlog"}]})
        if p.endswith("/space/S/folder"):
            return httpx.Response(
                200, json={"folders": [{"id": "F", "lists": [{"id": "L2", "name": "Sprint"}]}]}
            )
        return httpx.Response(404)

    r = await _client(handler, team_id="T").list_boards()
    assert [b["id"] for b in r["boards"]] == ["L1", "L2"]


def test_provider_switch(monkeypatch):
    from boardman import task_provider
    from boardman.clickup.client import ClickUpClient
    from boardman.plaky.client import PlakyClient

    monkeypatch.setattr(task_provider.settings, "task_provider", "clickup")
    assert isinstance(task_provider.get_task_client(), ClickUpClient)
    monkeypatch.setattr(task_provider.settings, "task_provider", "plaky")
    assert isinstance(task_provider.get_task_client(), PlakyClient)
    monkeypatch.setattr(task_provider.settings, "task_provider", "garbage")
    assert isinstance(task_provider.get_task_client(), PlakyClient)


@pytest.mark.parametrize("raw", ["", "  ", None])
def test_provider_defaults_to_plaky(monkeypatch, raw):
    from boardman import task_provider

    monkeypatch.setattr(task_provider.settings, "task_provider", raw)
    assert task_provider.active_provider() == "plaky"


def test_timeout_comes_from_settings_and_can_be_overridden(monkeypatch):
    monkeypatch.setattr("boardman.clickup.client.settings.clickup_api_timeout", 7.5)
    assert ClickUpClient("t", "https://cu.test").timeout == 7.5
    assert ClickUpClient("t", "https://cu.test", timeout=3).timeout == 3


async def test_get_tasks_warns_when_page_cap_is_hit(monkeypatch, caplog):
    monkeypatch.setattr("boardman.clickup.client._PAGE_CAP", 2)

    def handler(req):
        rows = [{"id": f"t{i}", "status": {"status": "to do"}} for i in range(100)]
        return httpx.Response(200, json={"tasks": rows, "last_page": False})

    with caplog.at_level("WARNING", logger="boardman.clickup.client"):
        r = await _client(handler).get_tasks("all")
    assert r["ok"] and len(r["tasks"]) == 200
    assert "truncated" in caplog.text
