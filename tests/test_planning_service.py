from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from boardman.planning.models import MeetingRequest
from boardman.planning.planner import MeetingPlanner
from boardman.planning.service import (
    default_plan_output_path,
    generate_plan,
    next_monday,
    week_anchor,
)


def test_next_monday_from_friday() -> None:
    # 2026-06-12 is a Friday
    assert next_monday(date(2026, 6, 12)) == date(2026, 6, 15)


def test_week_anchor_current_and_next() -> None:
    current = week_anchor("current")
    nxt = week_anchor("next")
    assert (nxt - current).days == 7


def test_week_anchor_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="week must be one of"):
        week_anchor("last")


def test_default_plan_output_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "boardman.planning.service.settings.planning_output_dir",
        str(tmp_path),
    )
    monkeypatch.setattr(
        "boardman.planning.service.week_anchor",
        lambda _week: date(2026, 6, 15),
    )
    path = default_plan_output_path("ai-ml", "weekly-status-sync", "next")
    assert path == tmp_path / "ai_ml_weekly_status_sync_2026-06-15.md"


class _StaticLlm:
    def generate(self, prompt: str):  # noqa: ANN001
        class R:
            text = """
## Purpose
Weekly QA sync on release readiness, regression coverage, and deployment gates for the sprint.

## Agenda Timeline
- 0:00–0:08 opening and outcomes
- 0:08–0:18 team snapshot on active streams
- 0:18–0:40 group round table on work, wins, and blockers
- 0:40–0:52 decisions and escalation paths
- 0:52–1:00 action read-back

## Group Round Table
Each participant shares what they are working on next, two wins from the week, and the top
blocker that needs a decision or owner today.

## Team Snapshot
Forum label: **QA:** core sync. Streams: regression automation, release checklist coverage,
and cross-team dependency on platform for staging stability.

## Decisions Needed
Confirm release gate owners and whether we defer non-critical defects to the next sprint.

## Risks and Blockers
Test env instability may delay sign-off; dependency risk if platform migration slips;
escalation path if incident volume spikes mid-week.

## Action Items
- [ ] Owner: QA lead — publish test matrix (due: Wed)
- [ ] Owner: Release manager — confirm rollback plan (due: Thu)

## Follow-up Checklist
- [ ] Notes posted to the shared doc
- [ ] Owners acknowledged actions in thread
"""
            provider = "stub"
            model = "stub-model"

        return R()


def test_generate_plan_returns_plan_without_write() -> None:
    request = MeetingRequest(
        meeting_title="Weekly",
        meeting_type="weekly-status-sync",
        team_focus="qa",
        attendees_count=10,
        objectives=["Align"],
        week_label="next-week",
        target_date_iso="2026-06-16",
    )
    planner = MeetingPlanner(llm=_StaticLlm())  # type: ignore[arg-type]
    plan = generate_plan(request, planner=planner)
    assert plan.provider_used == "stub"
    assert "## Purpose" in plan.markdown


def test_generate_plan_writes_output_file(tmp_path: Path) -> None:
    request = MeetingRequest(
        meeting_title="Weekly",
        meeting_type="weekly-status-sync",
        team_focus="qa",
        attendees_count=10,
        objectives=["Align"],
        week_label="next-week",
        target_date_iso="2026-06-16",
    )
    out = tmp_path / "plan.md"
    planner = MeetingPlanner(llm=_StaticLlm())  # type: ignore[arg-type]
    plan = generate_plan(request, output_path=out, planner=planner)
    assert out.exists()
    assert out.read_text(encoding="utf-8").strip() == plan.markdown.strip()
