"""ClickUp task creation, the CLI commands, scan filing and the deferred create job."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from boardman import task_provider
from boardman.cli import commands as cli
from boardman.jobs import handlers as jobs
from boardman.services import clickup_mutations as cm
from boardman.services import scan_handler as sh
from boardman.services import task_mutations as tm
from boardman.services.task_mutations import CreateSubtaskInput, CreateTaskInput
from boardman.settings import settings
from tests.clickup_fake import FakeClickUp

DEV = "12"
QA = "55"


@pytest.fixture
def cu(monkeypatch):
    from boardman.clickup.client import ClickUpClient

    fake = FakeClickUp()
    monkeypatch.setattr(task_provider.settings, "task_provider", "clickup")
    for name, value in {
        "needs_assigned": "to do",
        "assigned": "assigned",
        "completed": "complete",
    }.items():
        monkeypatch.setattr(settings, f"clickup_status_{name}", value)

    class Bound(ClickUpClient):
        def __init__(self, *a: Any, **k: Any) -> None:
            import httpx

            super().__init__(
                "tok", "https://cu.test/api/v2", transport=httpx.MockTransport(fake.handler)
            )

    monkeypatch.setattr(cm, "ClickUpClient", Bound)
    monkeypatch.setattr("boardman.clickup.client.ClickUpClient", Bound)
    monkeypatch.setattr(settings, "clickup_default_list_id", "")
    picks: list[str] = []

    async def pick(repo, cfg=None, **kw):
        picks.append(repo)
        return QA, "best fit"

    monkeypatch.setattr(cm, "pick_qa_for_repo", pick)
    monkeypatch.setattr(cm, "load_team_assignments", lambda: SimpleNamespace(members=[]))
    monkeypatch.setattr(
        "boardman.assignment.developer_eligibility.filter_developer",
        lambda pid, cfg=None: ("", "QA-only account") if pid == "99" else (pid, ""),
    )
    return SimpleNamespace(fake=fake, picks=picks, client=Bound())


def _post(cu) -> dict:
    return next(b for m, p, b in cu.fake.log if m == "POST" and p.endswith("/task"))


# -- create -------------------------------------------------------------------------------------------


async def test_create_goes_to_the_named_list_with_priority_tags_and_repo(cu):
    r = await tm.create_task_internal(
        CreateTaskInput(
            title="Add retries",
            description="why",
            priority="High",
            task_type="Bug",
            github_repos=["org/api"],
            plaky_board_id="L7",
            auto_assign_team=False,
        )
    )
    assert r["ok"] and r["task_id"] == "t1"
    post = _post(cu)
    assert post["name"] == "Add retries" and post["priority"] == 2
    assert post["description"].endswith("Repo: org/api") and post["tags"] == ["api", "type:bug"]
    assert any(p == "/list/L7/task" for _, p, _ in cu.fake.log)
    assert not cu.picks  # no QA at creation unless asked


async def test_status_follows_ownership_unless_one_is_named(cu):
    await tm.create_task_internal(
        CreateTaskInput(title="a", plaky_board_id="L1", engineer_plaky_id=DEV)
    )
    assert _post(cu)["status"] == "assigned" and _post(cu)["assignees"] == [12]
    cu.fake.log.clear()
    await tm.create_task_internal(CreateTaskInput(title="b", plaky_board_id="L1"))
    assert _post(cu)["status"] == "to do" and "assignees" not in _post(cu)
    cu.fake.log.clear()
    await tm.create_task_internal(
        CreateTaskInput(title="c", plaky_board_id="L1", status="In Review")
    )
    assert _post(cu)["status"] == "In Review"


async def test_an_ineligible_developer_is_refused_and_the_status_does_not_claim_an_owner(cu):
    r = await tm.create_task_internal(
        CreateTaskInput(title="a", plaky_board_id="L1", engineer_plaky_id="99")
    )
    assert r["ok"] and "QA-only" in r["developer_not_assigned"]
    assert _post(cu)["status"] == "to do" and "assignees" not in _post(cu)


async def test_qa_is_assigned_when_named_or_when_auto_and_a_repo_is_known(cu):
    r = await tm.create_task_internal(
        CreateTaskInput(title="a", plaky_board_id="L1", qa_plaky_id="77", auto_assign_team=False)
    )
    assert r["qa"]["ok"] and not cu.picks
    r = await tm.create_task_internal(
        CreateTaskInput(
            title="b", plaky_board_id="L1", github_repos=["org/api"], auto_assign_team=True
        )
    )
    assert cu.picks == ["org/api"] and r["qa"]["qa_user_id"] == int(QA)
    r = await tm.create_task_internal(
        CreateTaskInput(title="c", plaky_board_id="L1", auto_assign_team=True)
    )
    assert "qa" not in r  # no repo, nobody to pick for


async def test_the_list_falls_back_to_context_then_default_and_missing_is_a_clear_error(
    cu, monkeypatch
):
    r = await tm.create_task_internal(CreateTaskInput(title="a"))
    assert r["ok"] is False and r["status"] == 400 and "ClickUp list" in r["message"]
    monkeypatch.setattr(settings, "clickup_default_list_id", "LD")
    assert (await tm.create_task_internal(CreateTaskInput(title="b")))["ok"]
    assert any(p == "/list/LD/task" for _, p, _ in cu.fake.log)


async def test_filters_supply_what_the_fields_omit_and_a_title_is_required(cu):
    r = await tm.create_task_internal(
        CreateTaskInput(
            title="",
            priority="",  # the field wins when set; filters fill what is blank
            plaky_board_id="L1",
            filters={"title": "From filters", "priority": "low", "repo": "org/web"},
        )
    )
    assert r["ok"] and _post(cu)["name"] == "From filters" and _post(cu)["priority"] == 4
    assert (await tm.create_task_internal(CreateTaskInput(title="", plaky_board_id="L1")))[
        "status"
    ] == 400


async def test_a_failed_create_is_returned_not_hidden(cu):
    cu.fake.fail_create = True
    r = await tm.create_task_internal(CreateTaskInput(title="a", plaky_board_id="L1"))
    assert r["ok"] is False and r["status"] == 500


# -- subtasks -----------------------------------------------------------------------------------------


async def _parent(cu):
    cu.fake.tasks["p1"] = {
        "id": "p1",
        "name": "Parent",
        "status": {"status": "to do"},
        "list": {"id": "LP"},
        "assignees": [],
        "tags": [],
    }


async def test_a_subtask_lands_in_the_parents_list_with_owner_and_qa(cu):
    await _parent(cu)
    r = await tm.create_subtask_internal(
        CreateSubtaskInput(
            parent_task_id="p1",
            title="Child",
            engineer_plaky_id=DEV,
            qa_plaky_id="77",
            github_repos=["org/api"],
            auto_assign_qa=False,
        )
    )
    assert r["ok"] and r["parent_task_id"] == "p1"
    post = _post(cu)
    assert post["parent"] == "p1" and post["status"] == "assigned"
    assert any(p == "/list/LP/task" for _, p, _ in cu.fake.log)
    child = cu.fake.tasks[r["task_id"]]
    assert sorted(a["id"] for a in child["assignees"]) == ["12", "77"]


async def test_subtask_validation(cu):
    assert (await tm.create_subtask_internal(CreateSubtaskInput(parent_task_id="", title="x")))[
        "status"
    ] == 400
    assert (await tm.create_subtask_internal(CreateSubtaskInput(parent_task_id="p", title="")))[
        "status"
    ] == 400
    missing = await tm.create_subtask_internal(CreateSubtaskInput(parent_task_id="nope", title="x"))
    assert missing["ok"] is False


# -- scan filing --------------------------------------------------------------------------------------


def test_scan_text_includes_evidence_assumptions_and_unknowns():
    item = {
        "title": "Fix cache",
        "description": "stale reads",
        "priority": "HIGH",
        "evidence": ["commit abc"],
        "assumptions": ["redis is up"],
        "unknowns": ["load"],
    }
    title, body, pri = sh._scan_task_text(item, "api", "\n\n**Repo:** org/api\n")
    assert title == "[api] Fix cache" and pri == "high"
    assert (
        "**Evidence**\n- commit abc" in body
        and "**Assumptions**" in body
        and "**Unknowns**" in body
    )
    assert body.endswith("**Repo:** org/api")


async def test_clickup_scan_files_tasks_in_the_repos_list(cu):
    routing = SimpleNamespace(clickup_list_id="LS", category="backend")
    tasks = [{"title": "One", "description": "d", "priority": "high"}, {"title": "Two"}]
    created, warnings = await sh._file_scan_tasks_clickup(
        tasks, routing, short="api", repo_full="org/api", dry_run=False
    )
    assert (created, warnings) == (2, [])
    assert [p for m, p, _ in cu.fake.log if m == "POST"] == ["/list/LS/task"] * 2
    post = _post(cu)
    assert post["tags"] == ["api"] and "**Category:** backend" in post["description"]


async def test_clickup_scan_dry_run_creates_nothing_and_a_missing_list_says_why(cu):
    created, _ = await sh._file_scan_tasks_clickup(
        [{"title": "x"}],
        SimpleNamespace(clickup_list_id="LS"),
        short="a",
        repo_full="o/a",
        dry_run=True,
    )
    assert created == 0 and not cu.fake.log
    created, warnings = await sh._file_scan_tasks_clickup(
        [{"title": "x"}],
        SimpleNamespace(clickup_list_id=""),
        short="a",
        repo_full="o/a",
        dry_run=False,
    )
    assert created == 0 and "No ClickUp list for o/a" in warnings[0] and not cu.fake.log


# -- CLI ------------------------------------------------------------------------------------------------


def test_list_command_shows_clickup_tasks_with_their_status(cu):
    cu.fake.tasks["t9"] = {
        "id": "t9",
        "name": "Fix login",
        "status": {"status": "in progress"},
        "assignees": [],
        "tags": [],
    }

    class Lister:
        async def get_tasks(self, status="open", board_id=None):
            return {
                "ok": True,
                "tasks": [{**cu.fake.tasks["t9"], "status_name": "in progress"}],
            }

    cli.get_task_client = lambda: Lister()  # noqa: B010
    out = CliRunner().invoke(cli.app, ["list", "--board-id", "L1"])
    assert out.exit_code == 0
    assert "ClickUp Tasks" in out.output and "t9" in out.output and "in progress" in out.output


def test_link_pr_posts_the_comment_and_status_on_merge_uses_the_completed_status(cu, monkeypatch):
    seen: dict[str, Any] = {}

    class Commenter:
        async def add_comment(self, task_id, body, *, board_id=None):
            seen["comment"] = (task_id, body)
            return {"ok": True}

    async def fake_update(task_id, req):
        seen["status"] = (task_id, req.status)
        return {"ok": True}

    monkeypatch.setattr(cli, "get_task_client", lambda: Commenter())
    monkeypatch.setattr(cli, "update_task_internal", fake_update)
    out = CliRunner().invoke(
        cli.app,
        [
            "link-pr",
            "--pr-url",
            "https://github.com/o/r/pull/3",
            "--task-id",
            "t1",
            "--update-status",
        ],
    )
    assert out.exit_code == 0 and "PR linked successfully" in out.output
    assert seen["comment"][0] == "t1" and "pull/3" in seen["comment"][1]
    assert seen["status"] == ("t1", "complete")


def test_plaky_only_commands_explain_themselves_on_clickup(cu):
    for cmd in ("plaky-inventory", "capability-report"):
        out = CliRunner().invoke(cli.app, [cmd])
        assert out.exit_code == 1 and "not available on ClickUp" in out.output


def test_sync_needs_board_and_group_on_plaky_only(cu, monkeypatch):
    monkeypatch.setattr(task_provider.settings, "task_provider", "plaky")
    monkeypatch.setattr(cli, "github_auth_available", lambda: True)
    out = CliRunner().invoke(cli.app, ["sync", "--repo", "o/r"])
    assert out.exit_code == 1 and "required for Plaky" in out.output


def test_the_merge_status_and_provider_label_follow_the_provider(cu, monkeypatch):
    assert cli._provider_label() == "ClickUp" and cli._merge_status() == "complete"
    monkeypatch.setattr(task_provider.settings, "task_provider", "plaky")
    monkeypatch.setattr(cli.settings, "plaky_pr_merge_status", "Completed")
    assert cli._provider_label() == "Plaky" and cli._merge_status() == "Completed"


# -- deferred create job ---------------------------------------------------------------------------------


async def test_the_deferred_create_job_uses_the_clickup_batch_tool(cu, monkeypatch):
    from boardman.agent.tools import clickup_tools as ct

    monkeypatch.setattr(ct, "_client", lambda: cu.client)
    monkeypatch.setattr(ct.settings, "clickup_default_list_id", "")
    out = await jobs.plaky_create_tasks_job(
        {"tasks": [{"title": "One"}, {"title": "Two"}], "board_id": "LJ"}
    )
    assert out["created"] == 2 and out["already_present"] == 0
    assert [p for m, p, _ in cu.fake.log if m == "POST" and p.endswith("/task")] == [
        "/list/LJ/task"
    ] * 2
    bad = await jobs.plaky_create_tasks_job({"tasks": []})
    assert bad["ok"] is False
    assert json.dumps(out)
