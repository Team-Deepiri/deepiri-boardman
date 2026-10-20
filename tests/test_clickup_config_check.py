"""The startup check that catches a wrong ClickUp status name before it silently stalls a task."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from boardman.clickup import config_check as cc
from boardman.settings import settings

ALL = ["to do", "assigned", "needs qa", "in qa", "qa verified", "qa rejected", "complete"]


class StubClient:
    def __init__(self, lists: dict[str, list[str] | Exception | dict]):
        self.lists = lists
        self.asked: list[str] = []

    async def get_list(self, list_id: str) -> dict:
        self.asked.append(list_id)
        got = self.lists[list_id]
        if isinstance(got, Exception):
            raise got
        if isinstance(got, dict):
            return got
        return {"ok": True, "statuses": got}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "clickup_api_token", "pk_test")
    monkeypatch.setattr(settings, "clickup_default_list_id", "L1")
    for name, value in {
        "clickup_status_needs_assigned": "to do",
        "clickup_status_assigned": "assigned",
        "clickup_status_in_progress": "in progress",
        "clickup_status_needs_qa": "needs qa",
        "clickup_status_in_qa": "in qa",
        "clickup_status_approved": "qa verified",
        "clickup_status_changes_requested": "qa rejected",
        "clickup_status_completed": "complete",
    }.items():
        monkeypatch.setattr(settings, name, value, raising=False)
    monkeypatch.setattr(cc, "list_registered_repos", lambda: {})


async def test_a_list_with_every_status_is_clean():
    client = StubClient({"L1": ALL + ["in progress"]})
    assert await cc.clickup_config_problems(client) == []


async def test_a_missing_status_is_named_with_the_list_and_what_it_is_for():
    client = StubClient({"L1": [s for s in ALL if s != "qa rejected"] + ["in progress"]})
    problems = await cc.clickup_config_problems(client)
    assert len(problems) == 1
    assert (
        "L1" in problems[0]
        and "'qa rejected'" in problems[0]
        and "changes_requested" in problems[0]
    )


async def test_status_names_match_regardless_of_case():
    client = StubClient({"L1": [s.upper() for s in ALL + ["in progress"]]})
    assert await cc.clickup_config_problems(client) == []


async def test_every_repo_list_is_checked_not_just_the_default(monkeypatch):
    routings = {
        "org/a": SimpleNamespace(clickup_list_id="L2"),
        "org/b": SimpleNamespace(clickup_list_id="L1"),  # same as default: asked once
        "org/c": SimpleNamespace(clickup_list_id=""),
    }
    monkeypatch.setattr(cc, "list_registered_repos", lambda: routings)
    client = StubClient({"L1": ALL + ["in progress"], "L2": ["to do", "complete"]})
    problems = await cc.clickup_config_problems(client)
    assert client.asked == ["L1", "L2"]
    assert problems and all("L2" in p for p in problems)


async def test_an_unreadable_list_is_reported_not_raised():
    client = StubClient({"L1": {"ok": False, "message": "Team not authorized"}})
    (problem,) = await cc.clickup_config_problems(client)
    assert "L1" in problem and "Team not authorized" in problem
    boom = StubClient({"L1": RuntimeError("network down")})
    (problem,) = await cc.clickup_config_problems(boom)
    assert "network down" in problem


async def test_missing_token_and_missing_lists_are_called_out(monkeypatch):
    monkeypatch.setattr(settings, "clickup_api_token", "")
    assert "CLICKUP_API_TOKEN" in (await cc.clickup_config_problems(StubClient({})))[0]
    monkeypatch.setattr(settings, "clickup_api_token", "pk_test")
    monkeypatch.setattr(settings, "clickup_default_list_id", "")
    assert "No ClickUp list" in (await cc.clickup_config_problems(StubClient({})))[0]


async def test_problems_are_logged_as_warnings_and_a_clean_run_says_so(caplog):
    with caplog.at_level(logging.INFO, logger=cc._log.name):
        await cc.log_clickup_config_problems(StubClient({"L1": ["to do"]}))
        warned = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warned and "ClickUp config" in warned[0].getMessage()
        caplog.clear()
        await cc.log_clickup_config_problems(StubClient({"L1": ALL + ["in progress"]}))
        assert any("check out" in r.getMessage() for r in caplog.records)
