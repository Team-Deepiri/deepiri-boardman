from __future__ import annotations

import os
import re
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


def confine_to_output_dir(output_path: Path) -> Path:
    """Confine a plan output path to ``settings.planning_output_dir``.

    Guards every writer (CLI ``--output``, REST ``output_path``) against path
    traversal: the normalized target must stay within the configured output
    directory, otherwise ``ValueError`` is raised.

    Normalization uses pure string operations (``abspath``/``normpath``) rather
    than ``Path.resolve``/``os.path.realpath`` so the untrusted value never
    reaches a filesystem-touching call, and confinement is enforced with a
    ``startswith`` prefix check.
    """
    base = os.path.normpath(os.path.abspath(settings.planning_output_dir))
    target = os.path.normpath(os.path.abspath(os.fspath(output_path)))
    if not target.startswith(base + os.sep):
        raise ValueError(f"output_path escapes planning output directory: {output_path}")
    return Path(target)


def _safe_filename_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.]", "_", value)


def default_plan_output_path(team: str, meeting_type: str, week: str) -> Path:
    anchor = week_anchor(week).isoformat()
    safe_team = _safe_filename_component(team)
    safe_type = _safe_filename_component(meeting_type)
    out_dir = Path(settings.planning_output_dir)
    return confine_to_output_dir(out_dir / f"{safe_team}_{safe_type}_{anchor}.md")


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
        safe_path = confine_to_output_dir(output_path)
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        safe_path.write_text(plan.markdown + "\n", encoding="utf-8")
    return plan
