"""Agent tool: generate facilitator meeting plans from live org context."""

from __future__ import annotations

import asyncio
import json

from langchain_core.tools import StructuredTool

from boardman.planning.models import MeetingRequest
from boardman.planning.service import default_plan_output_path, generate_plan, week_anchor
from boardman.planning.team_repos import TEAM_CHOICES


async def _generate_meeting_plan(
    team_focus: str = "all-teams",
    week: str = "next",
    meeting_title: str = "Deepiri Weekly Engineering Round Table",
    meeting_type: str = "weekly-status-sync",
    notes: str = "",
    write_to_disk: bool = False,
) -> str:
    """Generate a facilitator markdown meeting plan using GitHub, Plaky, and boardman sync context."""
    normalized_team = team_focus.strip().lower()
    if normalized_team not in TEAM_CHOICES:
        return json.dumps(
            {
                "ok": False,
                "message": f"team_focus must be one of: {', '.join(TEAM_CHOICES)}",
            }
        )
    normalized_week = week.strip().lower()
    if normalized_week not in {"current", "next"}:
        return json.dumps({"ok": False, "message": "week must be current or next"})
    target = week_anchor(normalized_week)
    request = MeetingRequest(
        meeting_title=meeting_title,
        meeting_type=meeting_type,
        team_focus=normalized_team,
        attendees_count=15,
        objectives=[
            "Align weekly priorities across participating teams",
            "Surface wins and blockers quickly",
            "Assign ownership and due dates for every action",
        ],
        week_label=f"{normalized_week}-week",
        target_date_iso=target.isoformat(),
        notes=notes.strip() or None,
    )
    output_path = default_plan_output_path(normalized_team, meeting_type, normalized_week) if write_to_disk else None
    plan = await asyncio.to_thread(
        generate_plan,
        request,
        output_path=output_path,
    )
    return json.dumps(
        {
            "ok": True,
            "team_focus": normalized_team,
            "week": normalized_week,
            "target_date_iso": target.isoformat(),
            "provider_used": plan.provider_used,
            "model_used": plan.model_used,
            "generated_at_iso": plan.generated_at_iso,
            "output_path": str(output_path) if output_path else None,
            "markdown": plan.markdown[:12000],
        },
        indent=2,
    )


def generate_meeting_plan_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_generate_meeting_plan,
        name="generate_meeting_plan",
        description=(
            "Generate a facilitator-ready weekly meeting plan markdown for a team using live "
            "GitHub PRs, Plaky board items, boardman sync state, and repo DIRECTION context. "
            "Read-only unless write_to_disk is true. Args: team_focus (ai-ml, qa, "
            "frontend-backend-infra, it, all-teams), week (current|next), optional meeting_title, "
            "meeting_type, notes."
        ),
    )
