"""ClickUp agent tools: behavior against a mocked ClickUp API, plus provider-aware registry."""

from __future__ import annotations

import json

import httpx
import pytest

from boardman.agent.tools import clickup_tools as ct
from boardman.clickup.client import ClickUpClient

USERS = {
    "teams": [
        {
            "id": "T",
            "members": [
                {"user": {"id": 11, "username": "Ali Fahad", "email": "ali@x.io"}},
                {"user": {"id": 12, "username": "Sergio Vargas", "email": "sergio@x.io"}},
                {"user": {"id": 13, "username": "Ali Khan", "email": "alik@x.io"}},
            ],
        }
    ]
}


@pytest.fixture
def api(monkeypatch):
    """Route tool calls through a fake ClickUp. Returns the recorded requests."""
    log: list[tuple[str, str, dict]] = []
    state = {"existing": [], "n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else {}
        log.append((req.method, req.url.path, body))
        path = req.url.path
        if path.endswith("/team"):
            return httpx.Response(200, json=USERS)
        if req.method == "GET" and path.endswith("/list/L1/task"):
            return httpx.Response(200, json={"tasks": state["existing"], "last_page": True})
        if req.method == "POST" and path.endswith("/list/L1/task"):
            state["n"] += 1
            return httpx.Response(
                200, json={"id": f"new{state['n']}", "url": f"https://cu/new{state['n']}"}
            )
        if req.method == "PUT":
            return httpx.Response(200, json={"id": "t1", "name": "T", "status": {"status": "done"}})
        if path.endswith("/comment"):
            return httpx.Response(200, json={"id": 1})
        if req.method == "GET" and "/task/" in path:
            return httpx.Response(
                200,
                json={
                    "id": "p1",
                    "name": "Parent",
                    "list": {"id": "L1", "name": "Sprint"},
                    "description": "d",
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        ct,
        "_client",
        lambda: ClickUpClient(
            "tok", "https://cu.test/api/v2", default_list_id="L1", transport=transport
        ),
    )
    monkeypatch.setattr(ct.settings, "clickup_default_list_id", "L1")
    return log, state


async def test_create_tasks_dedupes_and_resolves_people(api):
    log, state = api
    state["existing"] = [{"id": "old", "name": "Fix the login bug", "status": {"status": "to do"}}]
    rows = [
        {"title": "Fix the login bug"},
        {"title": "Add retry", "assignee": "sergio", "priority": "high", "repo_tag": "org/repo"},
        {"title": "add RETRY"},  # same title inside one call
    ]
    out = json.loads(await ct._clickup_create_tasks(json.dumps(rows)))
    assert out["created"] == 1 and out["already_present"] == 1
    posts = [b for m, p, b in log if m == "POST" and p.endswith("/task")]
    assert len(posts) == 1
    assert posts[0]["assignees"] == [12] and posts[0]["priority"] == 2
    assert posts[0]["description"].endswith("Repo: org/repo")
    dup = next(r for r in out["receipts"] if r.get("duplicate"))
    assert dup["message"] == "Already in ClickUp" and dup["task_id"] == "old"


async def test_ambiguous_or_unknown_assignee_is_reported_not_guessed(api):
    log, _ = api
    out = json.loads(
        await ct._clickup_create_tasks(
            json.dumps(
                [
                    {"title": "A", "assignee": "ali"},
                    {"title": "B", "assignee": "nobody here"},
                ]
            )
        )
    )
    posts = [b for m, p, b in log if m == "POST" and p.endswith("/task")]
    assert all("assignees" not in b for b in posts)
    notes = [r["people_resolved"]["assignee"] for r in out["receipts"]]
    assert any("ambiguous" in n for n in notes) and any("no workspace member" in n for n in notes)


async def test_create_tasks_validates_input(api):
    assert json.loads(await ct._clickup_create_tasks("not json"))["status"] == 400
    assert json.loads(await ct._clickup_create_tasks("[]"))["status"] == 400


async def test_create_needs_a_list(api, monkeypatch):
    monkeypatch.setattr(ct.settings, "clickup_default_list_id", "")
    out = json.loads(await ct._clickup_create_tasks('[{"title":"x"}]'))
    assert out["ok"] is False and "list id" in out["message"]


async def test_list_tasks_reports_counts_and_truncation(api):
    _, state = api
    state["existing"] = [
        {
            "id": str(i),
            "name": f"t{i}",
            "status": {"status": "to do" if i % 2 else "done"},
            "assignees": [{"username": "ann"}] if i < 5 else [],
        }
        for i in range(70)
    ]
    out = json.loads(await ct._clickup_list_tasks("all"))
    assert out["total"] == 70 and out["returned"] == 60 and out["truncated"] is True
    assert out["count_by_status"] == {"to do": 35, "done": 35}
    assert out["with_owner_count"] == 5 and "Do NOT state" in out["note"]


async def test_update_task_adds_assignee_by_name(api):
    log, _ = api
    out = json.loads(await ct._clickup_update_task("t1", status="done", assignee="sergio"))
    assert out["ok"] is True
    put = next(b for m, p, b in log if m == "PUT")
    assert put["status"] == "done" and put["assignees"] == {"add": [12], "rem": []}


async def test_update_with_only_bad_assignee_fails_clearly(api):
    out = json.loads(await ct._clickup_update_task("t1", assignee="zzz"))
    assert out["ok"] is False and "no workspace member" in out["message"]


async def test_link_prs_and_comment(api):
    log, _ = api
    out = json.loads(
        await ct._clickup_link_prs(
            "t1", "https://github.com/o/r/pull/5, https://github.com/o/r/pull/6"
        )
    )
    assert out["ok"] and len(out["linked_pr_urls"]) == 2
    assert any(p.endswith("/task/t1/comment") for _, p, _ in log)
    assert json.loads(await ct._clickup_link_prs("t1", "nothing"))["status"] == 400


async def test_subtask_lands_in_parents_list_with_assignee(api):
    log, _ = api
    out = json.loads(await ct._clickup_create_subtask("p1", "Child", assignee="sergio"))
    assert out["ok"]
    post = next(b for m, p, b in log if m == "POST" and p.endswith("/list/L1/task"))
    assert post["parent"] == "p1"
    assert any(m == "PUT" and b.get("assignees") == {"add": [12], "rem": []} for m, _, b in log)


def test_registry_is_provider_aware(monkeypatch):
    from boardman import task_provider
    from boardman.agent import tools as tools_mod

    monkeypatch.setattr(task_provider.settings, "task_provider", "clickup")
    names = {t.name for t in tools_mod.build_all_tools(allow_writes=True)}
    assert "clickup_create_tasks" in names and "clickup_list_lists" in names
    assert not any(n.startswith("plaky_") for n in names)
    ro = {t.name for t in tools_mod.build_all_tools(allow_writes=False)}
    assert "clickup_create_tasks" not in ro and "clickup_list_tasks" in ro

    monkeypatch.setattr(task_provider.settings, "task_provider", "plaky")
    names = {t.name for t in tools_mod.build_all_tools(allow_writes=True)}
    assert "plaky_create_task" in names and not any(n.startswith("clickup_") for n in names)


def test_prompt_notice_names_the_tools_and_placement():
    from boardman.agent.clickup_prompt_extra import clickup_provider_markdown

    text = clickup_provider_markdown("L9")
    assert "clickup_create_tasks" in text and "`L9`" in text and "no** board schema" in text
    assert "not set" in clickup_provider_markdown(None)


async def test_list_limit_and_concurrency_come_from_settings(api, monkeypatch):
    _, state = api
    state["existing"] = [
        {"id": str(i), "name": f"t{i}", "status": {"status": "to do"}} for i in range(10)
    ]
    monkeypatch.setattr(ct.settings, "clickup_list_limit", 3)
    out = json.loads(await ct._clickup_list_tasks("all"))
    assert out["returned"] == 3 and out["total"] == 10 and out["truncated"] is True


def test_placement_accessor_is_provider_neutral():
    from boardman.agent import tool_context as tc

    assert tc.get_context_placement_id() == tc.get_context_plaky_board_id()
