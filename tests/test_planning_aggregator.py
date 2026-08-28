from __future__ import annotations

from boardman.planning.context_aggregator import ContextAggregator
from boardman.planning.huddle.models import MeetingRequest
from boardman.planning.huddle.planner import MeetingPlanner


class _StubContext:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[str] = []

    def context_markdown(self, team_focus: str) -> str:
        self.calls.append(team_focus)
        return self._text


class _FailingContext:
    def context_markdown(self, team_focus: str) -> str:
        raise RuntimeError("provider down")


def test_aggregator_merges_all_sections_in_order() -> None:
    aggregator = ContextAggregator(
        github_context=_StubContext("## GitHub Pull Requests\n- PR #1"),
        plaky_context=_StubContext("## Plaky Board Items\n- Task A"),
        sync_context=_StubContext("## Boardman Sync State\n- link 1"),
        direction_context=_StubContext("## Repo Direction\n- focus on reliability"),
    )
    md = aggregator.context_markdown("qa")
    github_pos = md.index("GitHub Pull Requests")
    plaky_pos = md.index("Plaky Board Items")
    sync_pos = md.index("Boardman Sync State")
    direction_pos = md.index("Repo Direction")
    assert github_pos < plaky_pos < sync_pos < direction_pos


def test_aggregator_survives_single_provider_failure() -> None:
    aggregator = ContextAggregator(
        github_context=_StubContext("## GitHub Pull Requests\n- ok"),
        plaky_context=_FailingContext(),
        sync_context=_StubContext("## Boardman Sync State\n- ok"),
        direction_context=_StubContext("## Repo Direction\n- ok"),
    )
    md = aggregator.context_markdown("ai-ml")
    assert "## GitHub Pull Requests" in md
    assert "## Plaky" in md
    assert "unavailable" in md
    assert "## Boardman Sync State" in md
    assert "## Repo Direction" in md


def test_planner_prompt_includes_aggregated_context() -> None:
    aggregator = ContextAggregator(
        github_context=_StubContext("## GitHub Pull Requests\n- stub"),
        plaky_context=_StubContext("## Plaky Board Items\n- stub"),
        sync_context=_StubContext("## Boardman Sync State\n- stub"),
        direction_context=_StubContext("## Repo Direction\n- stub"),
    )
    planner = MeetingPlanner(llm=None, context_aggregator=aggregator)  # type: ignore[arg-type]
    request = MeetingRequest(
        meeting_title="Weekly",
        meeting_type="weekly-status-sync",
        team_focus="qa",
        attendees_count=10,
        objectives=["Align"],
        week_label="next-week",
        target_date_iso="2026-06-16",
    )
    prompt = planner._build_prompt(request)
    assert "Organizational context (GitHub, Plaky, boardman sync, repo direction):" in prompt
    assert "## GitHub Pull Requests" in prompt
    assert "## Plaky Board Items" in prompt
    assert "## Boardman Sync State" in prompt
    assert "## Repo Direction" in prompt
