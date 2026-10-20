"""Meeting-plan context from ClickUp lists, and the provider-aware labels around it."""

from __future__ import annotations

import time

import pytest

from boardman import task_provider
from boardman.clickup.client import ClickUpClient
from boardman.planning.context_aggregator import ContextAggregator
from boardman.planning.huddle import context_clickup as cc
from boardman.planning.huddle.context_clickup import ClickUpPlanningContext, normalize_task
from boardman.planning.huddle.context_plaky import PlakyPlanningContext
from boardman.planning.huddle.context_sync import _format_issue_maps, _format_pr_links
from boardman.planning.team_models import PlakyBoardRef


def _ms(days_ago: float) -> str:
    return str(int((time.time() - days_ago * 86400) * 1000))


class _Stub:
    def __init__(self, text: str) -> None:
        self.text = text

    def context_markdown(self, team_focus: str) -> str:
        return self.text


@pytest.fixture
def clickup(monkeypatch):
    monkeypatch.setattr(task_provider.settings, "task_provider", "clickup")
    monkeypatch.setattr(cc.settings, "clickup_api_token", "tok")
    tasks = [
        {
            "id": "1",
            "name": "Fix login",
            "status": {"status": "in progress"},
            "status_name": "in progress",
            "assignees": [{"id": 1, "username": "ann"}, {"id": 2, "email": "bob@x.io"}],
            "date_updated": _ms(1),
        },
        {
            "id": "2",
            "name": "Old thing",
            "status": {"status": "to do"},
            "status_name": "to do",
            "assignees": [],
            "date_updated": _ms(60),
        },
        {
            "id": "3",
            "name": "Needs eyes",
            "status": {"status": "needs qa"},
            "assignees": [],
            "date_updated": _ms(2),
        },
    ]
    seen: list[str] = []

    async def fake_get_tasks(self, status="open", board_id=None):
        seen.append(f"{status}:{board_id}")
        return {"ok": True, "tasks": tasks}

    monkeypatch.setattr(ClickUpClient, "get_tasks", fake_get_tasks)
    ctx = ClickUpPlanningContext()
    ctx._team_boards = {"eng": PlakyBoardRef(board_id="LIST1")}
    return ctx, seen


def test_a_clickup_task_is_normalized_for_the_shared_summary_helpers():
    n = normalize_task(
        {
            "id": "9",
            "name": "T",
            "status": {"status": "done"},
            "assignees": [{"username": "ann"}, {"email": "b@x.io"}, {}],
            "date_updated": "1700000000000",
        }
    )
    assert n["title"] == "T" and n["status"] == "done" and n["assignees"] == ["ann", "b@x.io"]
    assert n["updatedAt"].startswith("2023-11-14")
    assert normalize_task({"name": "x"})["updatedAt"] == "" and normalize_task({})["status"] == ""


def test_recent_items_come_from_the_teams_list_and_old_ones_are_dropped(clickup):
    ctx, seen = clickup
    items = ctx.fetch_recent_items("eng")
    assert seen == ["all:LIST1"]
    assert [i.title for i in items] == ["Fix login", "Needs eyes"]  # the 60-day-old task is out
    first = items[0]
    assert first.status == "in progress" and first.assignees == "ann, bob@x.io"
    assert first.board_label == "list=LIST1"


def test_the_markdown_says_clickup_and_highlights_statuses(clickup):
    ctx, _ = clickup
    md = ctx.context_markdown("eng")
    assert md.startswith("## ClickUp List Items (last 14 days)")
    assert "### in progress (1)" in md and "Needs eyes" in md and "Plaky" not in md
    assert md.index("### in progress") < md.index("### needs qa")  # both are highlighted


def test_unconfigured_unmapped_and_empty_cases_name_clickup(clickup, monkeypatch):
    ctx, _ = clickup
    monkeypatch.setattr(cc.settings, "clickup_api_token", "")
    assert ctx.context_markdown("eng") == "ClickUp not configured (set CLICKUP_API_TOKEN)."
    monkeypatch.setattr(cc.settings, "clickup_api_token", "tok")
    assert "No ClickUp list mapped for this team" in ctx.context_markdown("nobody")

    async def empty(self, status="open", board_id=None):
        return {"ok": True, "tasks": []}

    monkeypatch.setattr(ClickUpClient, "get_tasks", empty)
    assert "No ClickUp items updated in the last 14 days" in ctx.context_markdown("eng")


def test_a_failing_list_is_skipped_not_fatal(clickup, monkeypatch):
    ctx, _ = clickup

    async def boom(self, status="open", board_id=None):
        return {"ok": False, "message": "rate limited"}

    monkeypatch.setattr(ClickUpClient, "get_tasks", boom)
    assert ctx.fetch_recent_items("eng") == []


def test_the_aggregator_uses_the_clickup_context_only_on_clickup(monkeypatch):
    monkeypatch.setattr(task_provider.settings, "task_provider", "clickup")
    agg = ContextAggregator(
        github_context=_Stub("g"), sync_context=_Stub("s"), direction_context=_Stub("d")
    )
    labels = [label for label, _ in agg._sources]
    assert labels == ["GitHub", "ClickUp", "Boardman sync", "Repo direction"]
    assert isinstance(agg._sources[1][1], ClickUpPlanningContext)
    monkeypatch.setattr(task_provider.settings, "task_provider", "plaky")
    agg = ContextAggregator(
        github_context=_Stub("g"), sync_context=_Stub("s"), direction_context=_Stub("d")
    )
    assert agg._sources[1][0] == "Plaky" and type(agg._sources[1][1]) is PlakyPlanningContext


def test_an_injected_task_context_is_still_honoured_on_clickup(monkeypatch):
    monkeypatch.setattr(task_provider.settings, "task_provider", "clickup")
    agg = ContextAggregator(
        github_context=_Stub("g"),
        plaky_context=_Stub("## ClickUp List Items\n- stub"),
        sync_context=_Stub("s"),
        direction_context=_Stub("d"),
    )
    assert "- stub" in agg.context_markdown("eng")


def test_the_sync_context_headings_follow_the_provider(monkeypatch):
    from types import SimpleNamespace

    link = SimpleNamespace(
        plaky_task_id="t1", repo="r", pr_number=3, merged=False, withdrawn=False, link_source="x"
    )
    imap = SimpleNamespace(plaky_task_url="", plaky_task_id="t1", repo="r", issue_number=7)
    monkeypatch.setattr(task_provider.settings, "task_provider", "clickup")
    text = "\n".join(_format_pr_links([link]) + _format_issue_maps([imap]))
    assert "PR ↔ ClickUp task links" in text and "Issue ↔ ClickUp mappings" in text
    assert "ClickUp `t1`" in text and "Plaky" not in text
    monkeypatch.setattr(task_provider.settings, "task_provider", "plaky")
    text = "\n".join(_format_pr_links([link]) + _format_issue_maps([imap]))
    assert "PR ↔ Plaky task links" in text and "→ Plaky `t1`" in text
