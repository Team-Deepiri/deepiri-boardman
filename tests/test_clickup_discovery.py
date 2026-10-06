"""The /plaky/* discovery routes and the inventory command on ClickUp."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from boardman import task_provider
from boardman.cli import commands as cli
from boardman.clickup import statuses as st
from boardman.clickup.client import ClickUpClient
from boardman.routes import plaky as routes

TEAMS = {
    "teams": [{"id": "T", "members": [{"user": {"id": 5, "username": "Ann", "email": "a@x.io"}}]}]
}


def _handler(req: httpx.Request) -> httpx.Response:
    p = req.url.path.removeprefix("/api/v2")
    if p == "/team":
        return httpx.Response(200, json=TEAMS)
    if p == "/team/T/space":
        return httpx.Response(200, json={"spaces": [{"id": "S"}]})
    if p == "/space/S/list":
        return httpx.Response(
            200, json={"lists": [{"id": "L1", "name": "Backlog"}, {"id": "L2", "name": "Sprint"}]}
        )
    if p == "/space/S/folder":
        return httpx.Response(200, json={"folders": []})
    if p == "/list/L1":
        return httpx.Response(
            200,
            json={
                "name": "Backlog",
                "statuses": [
                    {"status": "to do"},
                    {"status": "in progress"},
                    {"status": "complete"},
                ],
            },
        )
    return httpx.Response(404, text="no such thing")


@pytest.fixture
def cu(monkeypatch):
    monkeypatch.setattr(task_provider.settings, "task_provider", "clickup")
    monkeypatch.setattr(cli.settings, "clickup_api_token", "tok")

    class Bound(ClickUpClient):
        def __init__(self, *a, **k):
            super().__init__(
                "tok",
                "https://cu.test/api/v2",
                team_id="T",
                transport=httpx.MockTransport(_handler),
            )

    monkeypatch.setattr("boardman.clickup.client.ClickUpClient", Bound)
    monkeypatch.setattr(
        task_provider,
        "get_task_client",
        lambda: Bound() if task_provider.active_provider() == "clickup" else None,
    )
    monkeypatch.setattr(routes, "get_task_client", lambda: Bound())
    for name, value in {
        "needs_assigned": "to do",
        "assigned": "to do",
        "in_progress": "in progress",
        "completed": "complete",
        "needs_qa": "needs qa",
    }.items():
        monkeypatch.setattr(st.settings, f"clickup_status_{name}", value)
    for name in ("paused", "in_qa", "approved", "changes_requested", "deployed"):
        monkeypatch.setattr(st.settings, f"clickup_status_{name}", "")
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def test_users_and_boards_come_from_clickup_in_the_same_shape(cu):
    users = cu.get("/plaky/users").json()
    assert users["ok"] and users["users"][0]["id"] == "5"
    ranked = cu.get("/plaky/users", params={"query": "ann"}).json()
    assert ranked["best"]["id"] == "5"
    boards = cu.get("/plaky/boards").json()
    assert [b["id"] for b in boards["boards"]] == ["L1", "L2"]
    match = cu.get("/plaky/boards/match", params={"query": "sprint"}).json()
    assert match["best"]["id"] == "L2"


def test_groups_do_not_exist_on_clickup_and_say_so(cu):
    g = cu.get("/plaky/boards/L1/groups").json()
    assert g["ok"] is True and g["groups"] == [] and "no groups" in g["message"]
    m = cu.get("/plaky/boards/L1/groups/match", params={"query": "Backlog"}).json()
    assert m["groups"] == [] and m["best"] is None and m["matches"] == []


def test_the_schema_is_the_lists_statuses(cu):
    r = cu.get("/plaky/boards/L1/schema").json()
    assert r["ok"] and r["board_id"] == "L1"
    field = r["normalized"]["fields"][0]
    assert field["type"] == "status" and [o["name"] for o in field["options"]] == [
        "to do",
        "in progress",
        "complete",
    ]
    assert "ClickUp list: Backlog" in r["markdown"] and "- in progress" in r["markdown"]
    bad = cu.get("/plaky/boards/NOPE/schema").json()
    assert bad["ok"] is False and bad["normalized"] is None


def test_the_plaky_routes_are_untouched_when_plaky_is_the_provider(monkeypatch):
    monkeypatch.setattr(task_provider.settings, "task_provider", "plaky")
    seen = {}

    class FakePlaky:
        async def list_groups(self, board_id):
            seen["groups"] = board_id
            return {"ok": True, "groups": [{"id": "g", "name": "G"}]}

    monkeypatch.setattr(routes, "PlakyClient", FakePlaky)
    app = FastAPI()
    app.include_router(routes.router)
    r = TestClient(app).get("/plaky/boards/B1/groups").json()
    assert seen == {"groups": "B1"} and r["groups"][0]["id"] == "g"


def test_inventory_lists_members_and_lists_and_checks_the_statuses(cu):
    out = CliRunner().invoke(cli.app, ["clickup-inventory", "--list-id", "L1"])
    assert "Workspace members" in out.output and "Ann" in out.output
    assert "Backlog" in out.output and "Sprint" in out.output
    assert "List statuses: to do, in progress, complete" in out.output
    # needs_qa is configured as "needs qa" but the list has no such status
    assert out.exit_code == 1 and "workflow_needs_qa" in out.output and "'needs qa'" in out.output


def test_inventory_passes_when_every_configured_status_exists(cu, monkeypatch):
    monkeypatch.setattr(st.settings, "clickup_status_needs_qa", "")
    out = CliRunner().invoke(cli.app, ["clickup-inventory", "--list-id", "L1"])
    assert out.exit_code == 0 and "Every configured CLICKUP_STATUS_* name exists" in out.output


def test_inventory_without_a_token_or_with_a_bad_list_fails_clearly(cu, monkeypatch):
    bad = CliRunner().invoke(cli.app, ["clickup-inventory", "--list-id", "NOPE"])
    assert bad.exit_code == 1 and "ClickUp error" in bad.output
    monkeypatch.setattr(cli.settings, "clickup_api_token", "")
    none = CliRunner().invoke(cli.app, ["clickup-inventory"])
    assert none.exit_code == 1 and "CLICKUP_API_TOKEN is not set" in none.output


def test_configured_statuses_lists_each_setting_once(cu, monkeypatch):
    monkeypatch.setattr(st.settings, "clickup_status_needs_qa", "needs qa")
    names = dict(st.configured_statuses())
    assert names["workflow_needs_qa"] == "needs qa" and "workflow_needs_qa_again" not in names
    assert (
        list(names.values()).count("to do") == 2
    )  # needs-assigned and assigned are separate settings


def test_plaky_inventory_on_clickup_shows_the_clickup_inventory(cu):
    out = CliRunner().invoke(cli.app, ["plaky-inventory", "--board-id", "L1"])
    assert "Workspace members" in out.output and "List statuses: to do" in out.output
