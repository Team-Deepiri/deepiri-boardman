"""ClickUp QA assignment: roster ids from ClickUp, QA application, and the update entry point."""

from __future__ import annotations

import json

import httpx
import pytest
import yaml

from boardman.assignment import config
from boardman.clickup.client import ClickUpClient
from boardman.services import clickup_mutations as cm
from boardman.services.task_mutations import UpdateTaskInput


def _client(handler, **kw) -> ClickUpClient:
    return ClickUpClient(
        "tok", "https://cu.test/api/v2", transport=httpx.MockTransport(handler), **kw
    )


# -- client helpers -------------------------------------------------------------------------


async def test_assign_qa_adds_assignee_when_no_field_configured(monkeypatch):
    monkeypatch.setattr("boardman.clickup.client.settings.clickup_qa_field_id", "")
    seen = {}

    def handler(req):
        seen["m"], seen["p"], seen["b"] = req.method, req.url.path, json.loads(req.content)
        return httpx.Response(200, json={"id": "t1"})

    r = await _client(handler).assign_qa("t1", "12")
    assert r["ok"] and r["via"] == "assignee"
    assert seen["m"] == "PUT" and seen["b"] == {"assignees": {"add": [12], "rem": []}}


async def test_assign_qa_uses_users_field_when_configured():
    seen = {}

    def handler(req):
        seen["m"], seen["p"], seen["b"] = req.method, req.url.path, json.loads(req.content)
        return httpx.Response(200, json={})

    r = await _client(handler).assign_qa("t1", "12", qa_field_id="FLD")
    assert r["ok"] and r["via"] == "custom_field"
    assert seen["m"] == "POST" and seen["p"].endswith("/task/t1/field/FLD")
    assert seen["b"] == {"value": {"add": [12], "rem": []}}


async def test_assign_qa_rejects_non_numeric_user_id():
    r = await _client(lambda req: httpx.Response(500)).assign_qa("t1", "plaky-alice")
    assert r["ok"] is False and r["status"] == 400


def test_list_workspace_users_sync():
    teams = {
        "teams": [
            {"id": "1", "members": [{"user": {"id": 5, "username": "Ann", "email": "a@x.io"}}]}
        ]
    }
    r = _client(lambda req: httpx.Response(200, json=teams)).list_workspace_users_sync()
    assert r["ok"] and r["users"][0] == {
        "id": "5",
        "name": "Ann",
        "email": "a@x.io",
        "github_login": None,
    }
    assert ClickUpClient("", "https://cu.test").list_workspace_users_sync()["status"] == 400


# -- roster ids come from ClickUp -----------------------------------------------------------


def test_roster_matches_github_members_to_clickup_user_ids(tmp_path, monkeypatch):
    from boardman import task_provider

    yml = tmp_path / "ta.yml"
    yml.write_text(
        yaml.dump({"member_defaults": {"repo_globs": ["org/*"], "roles": ["qa"]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config.settings, "team_assignments_yml_path", str(yml))
    config._raw.cache_clear()
    monkeypatch.setattr(task_provider.settings, "task_provider", "clickup")
    monkeypatch.setattr(
        "boardman.assignment.config.get_cached_support_team_roster",
        lambda spec: {"ok": True, "members": [{"login": "alicesmith", "name": "Alice Smith"}]},
    )
    monkeypatch.setattr(
        ClickUpClient,
        "list_workspace_users_sync",
        lambda self: {
            "ok": True,
            "users": [
                {"id": "42", "name": "Alice Smith", "email": "alice@x.io", "github_login": None},
                {"id": "43", "name": "Bob Jones", "email": "bob@x.io", "github_login": None},
            ],
        },
    )
    cfg = config.load_team_assignments(refresh=True)
    assert [(m.github_login, m.id) for m in cfg.members] == [("alicesmith", "42")]


# -- update entry point ---------------------------------------------------------------------


@pytest.fixture
def cu(monkeypatch):
    log: list[tuple[str, str, dict]] = []

    def handler(req):
        log.append((req.method, req.url.path, json.loads(req.content) if req.content else {}))
        return httpx.Response(200, json={"id": "t1", "name": "x"})

    client = _client(handler)
    monkeypatch.setattr(cm, "ClickUpClient", lambda: client)
    monkeypatch.setattr("boardman.clickup.client.settings.clickup_qa_field_id", "")
    return log


async def test_update_sets_fields_and_explicit_qa(cu):
    r = await cm.update_clickup_task(
        "t1", UpdateTaskInput(status="in review", priority="high", qa_plaky_id="12")
    )
    assert r["ok"] and set(r["operations"]) == {"task_fields", "qa"}
    puts = [b for m, _, b in cu if m == "PUT"]
    assert puts[0] == {"priority": 2, "status": "in review"}
    assert puts[1]["assignees"] == {"add": [12], "rem": []}


async def test_update_auto_assigns_qa_from_repo(cu, monkeypatch):
    async def fake_pick(repo, cfg=None, **_):
        assert repo == "org/web"
        return "77", "best fit"

    monkeypatch.setattr(cm, "pick_qa_for_repo", fake_pick)
    monkeypatch.setattr(cm, "load_team_assignments", lambda: object())
    r = await cm.update_clickup_task(
        "t1", UpdateTaskInput(auto_assign_qa=True, github_repo="org/web")
    )
    assert r["ok"] and r["operations"]["qa_auto_assign"]["picked_qa_user_id"] == "77"
    assert any(b.get("assignees") == {"add": [77], "rem": []} for _, _, b in cu)


async def test_update_auto_assign_failure_is_reported_and_writes_nothing(cu, monkeypatch):
    async def none_pick(repo, cfg=None, **_):
        return None, "no eligible QA"

    monkeypatch.setattr(cm, "pick_qa_for_repo", none_pick)
    monkeypatch.setattr(cm, "load_team_assignments", lambda: object())
    r = await cm.update_clickup_task(
        "t1", UpdateTaskInput(status="done", auto_assign_qa=True, github_repo="org/web")
    )
    assert r["ok"] is False and "no eligible QA" in r["message"]
    assert cu == []


async def test_update_validation(cu):
    assert (await cm.update_clickup_task("t1", UpdateTaskInput()))["status"] == 400
    r = await cm.update_clickup_task("t1", UpdateTaskInput(auto_assign_qa=True))
    assert r["status"] == 400 and "github_repo" in r["message"]
    r = await cm.update_clickup_task("t1", UpdateTaskInput(engineer_plaky_id="5"))
    assert r["ok"] is False and "not supported" in r["message"]
    assert cu == []


async def test_update_skips_task_type_and_reports_api_failure(monkeypatch):
    client = _client(lambda req: httpx.Response(400, text="bad status"))
    monkeypatch.setattr(cm, "ClickUpClient", lambda: client)
    r = await cm.update_clickup_task("t1", UpdateTaskInput(status="nope", task_type="bug"))
    assert r["ok"] is False
    assert r["operations"]["task_type"]["skipped"] is True
    assert r["operations"]["task_fields"]["status"] == 400


async def test_update_task_internal_dispatches_to_clickup(cu, monkeypatch):
    from boardman import task_provider
    from boardman.services.task_mutations import update_task_internal

    monkeypatch.setattr(task_provider.settings, "task_provider", "clickup")
    r = await update_task_internal("t1", UpdateTaskInput(status="done"))
    assert r["ok"] and "task_fields" in r["operations"]
    assert [m for m, _, _ in cu] == ["PUT"]


async def test_agent_tool_qa_by_name_and_auto(monkeypatch):
    from boardman.agent.tools import clickup_tools as ct

    users = {
        "teams": [
            {
                "id": "T",
                "members": [{"user": {"id": 12, "username": "Sergio Vargas", "email": "s@x.io"}}],
            }
        ]
    }
    log = []

    def handler(req):
        log.append((req.method, req.url.path, json.loads(req.content) if req.content else {}))
        if req.url.path.endswith("/team"):
            return httpx.Response(200, json=users)
        return httpx.Response(200, json={"id": "t1", "name": "x"})

    client = _client(handler)
    monkeypatch.setattr(ct, "_client", lambda: client)
    monkeypatch.setattr(cm, "ClickUpClient", lambda: client)
    monkeypatch.setattr("boardman.clickup.client.settings.clickup_qa_field_id", "")

    out = json.loads(await ct._clickup_update_task("t1", status="in review", qa="sergio"))
    assert out["ok"] and any(b.get("assignees") == {"add": [12], "rem": []} for _, _, b in log)

    out = json.loads(await ct._clickup_update_task("t1", qa="nobody"))
    assert out["ok"] is False and "no workspace member" in out["message"]
