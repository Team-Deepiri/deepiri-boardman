from __future__ import annotations

import json
from pathlib import Path

import pytest

from boardman.agent.tools.planning_tools import generate_meeting_plan_tool
from boardman.planning.models import MeetingPlan


@pytest.mark.asyncio
async def test_generate_meeting_plan_tool_returns_markdown(monkeypatch):
    def _fake_generate_plan(request, **kwargs):  # noqa: ANN001
        return MeetingPlan(
            markdown="## Purpose\nWeekly sync.\n",
            provider_used="stub",
            model_used="stub-model",
            generated_at_iso="2026-06-16T12:00:00+00:00",
        )

    monkeypatch.setattr("boardman.agent.tools.planning_tools.generate_plan", _fake_generate_plan)
    tool = generate_meeting_plan_tool()
    raw = await tool.ainvoke({"team_focus": "qa", "week": "next"})
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["team_focus"] == "qa"
    assert payload["provider_used"] == "stub"
    assert "Weekly sync" in payload["markdown"]


@pytest.mark.asyncio
async def test_generate_meeting_plan_tool_rejects_invalid_team():
    tool = generate_meeting_plan_tool()
    raw = await tool.ainvoke({"team_focus": "not-a-team"})
    payload = json.loads(raw)
    assert payload["ok"] is False
