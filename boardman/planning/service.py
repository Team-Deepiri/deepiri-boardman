from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from boardman.planning.context_aggregator import ContextAggregator
from boardman.planning.llm_adapter import BoardmanPlanningLlm
from boardman.planning.models import MeetingPlan, MeetingRequest
from boardman.planning.planner import MeetingPlanner
from boardman.settings import settings

WEEK_CHOICES = ("current", "next")


def next_monday(anchor: date) -> date:
    days = (7 - anchor.weekday()) % 7
    if days == 0:
        days = 7
    return anchor + timedelta(days=days)


def week_anchor(week: str) -> date:
    if week not in WEEK_CHOICES:
        raise ValueError(f"week must be one of: {', '.join(WEEK_CHOICES)}")
    base = next_monday(date.today())
    return base if week == "current" else base + timedelta(days=7)


def default_plan_output_path(team: str, meeting_type: str, week: str) -> Path:
    anchor = week_anchor(week).isoformat()
    safe_team = team.replace("-", "_")
    safe_type = meeting_type.replace("-", "_")
    out_dir = Path(settings.planning_output_dir)
    return out_dir / f"{safe_team}_{safe_type}_{anchor}.md"


def build_planner(
    *,
    provider: str | None = None,
    model: str | None = None,
    planner: MeetingPlanner | None = None,
) -> MeetingPlanner:
    if planner is not None:
        return planner
    return MeetingPlanner(
        llm=BoardmanPlanningLlm(provider=provider, model=model),
        context_aggregator=ContextAggregator(),
    )


def generate_plan(
    request: MeetingRequest,
    *,
    output_path: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    planner: MeetingPlanner | None = None,
) -> MeetingPlan:
    """Generate a meeting plan; optionally write markdown to disk."""
    active_planner = build_planner(provider=provider, model=model, planner=planner)
    plan = active_planner.plan(request)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(plan.markdown + "\n", encoding="utf-8")
    return plan
