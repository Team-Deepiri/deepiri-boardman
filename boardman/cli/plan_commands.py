from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.status import Status

from boardman.planning.context_github import GitHubPlanningContext
from boardman.planning.context_plaky import PlakyPlanningContext
from boardman.planning.llm_adapter import BoardmanPlanningLlm
from boardman.planning.models import MeetingRequest
from boardman.planning.planner import MeetingPlanner
from boardman.planning.team_repos import TEAM_CHOICES
from boardman.settings import settings

plan_app = typer.Typer(help="Generate weekly meeting plans (from deepiri-huddle)")
console = Console()
WEEK_CHOICES = ("current", "next")


def _next_monday(anchor: date) -> date:
    days = (7 - anchor.weekday()) % 7
    if days == 0:
        days = 7
    return anchor + timedelta(days=days)


def _week_anchor(week: str) -> date:
    base = _next_monday(date.today())
    return base if week == "current" else base + timedelta(days=7)


def _default_output(team: str, meeting_type: str, week: str) -> Path:
    anchor = _week_anchor(week).isoformat()
    safe_team = team.replace("-", "_")
    safe_type = meeting_type.replace("-", "_")
    out_dir = Path(settings.planning_output_dir)
    return out_dir / f"{safe_team}_{safe_type}_{anchor}.md"


def _build_planner(
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> MeetingPlanner:
    return MeetingPlanner(
        llm=BoardmanPlanningLlm(provider=provider, model=model),
        github_context=GitHubPlanningContext(),
        plaky_context=PlakyPlanningContext(),
    )


def _generate_and_write(
    request: MeetingRequest,
    output: Path,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> None:
    planner = _build_planner(provider=provider, model=model)
    with Status("Generating meeting plan...", spinner="dots", console=console):
        plan = planner.plan(request)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(plan.markdown + "\n", encoding="utf-8")
    console.print(
        Panel.fit(
            f"Wrote plan to [bold]{output}[/bold]\n"
            f"Provider: [bold]{plan.provider_used}[/bold]\n"
            f"Model: [bold]{plan.model_used}[/bold]\n"
            f"Generated: {plan.generated_at_iso}",
            title="deepiri-boardman plan",
        )
    )


@plan_app.command()
def weekly(
    team: str = typer.Option(
        "all-teams",
        "--team",
        "-t",
        prompt="Which team is this meeting for? (ai-ml, qa, frontend-backend-infra, it, all-teams)",
    ),
    week: str = typer.Option("next", "--week", help="current or next"),
    output: Path | None = typer.Option(None, "--output", "-o"),
    provider: Optional[str] = typer.Option(None, "--provider", help="LLM provider override"),
    model: Optional[str] = typer.Option(None, "--model", help="LLM model override"),
) -> None:
    """Generate a weekly engineering round-table plan."""
    if team not in TEAM_CHOICES:
        raise typer.BadParameter(f"team must be one of: {', '.join(TEAM_CHOICES)}")
    if week not in WEEK_CHOICES:
        raise typer.BadParameter(f"week must be one of: {', '.join(WEEK_CHOICES)}")
    target = _week_anchor(week)
    req = MeetingRequest(
        meeting_title="Deepiri Weekly Engineering Round Table",
        meeting_type="weekly-status-sync",
        team_focus=team,
        attendees_count=15,
        objectives=[
            "Align weekly priorities across participating teams",
            "Surface wins and blockers quickly",
            "Assign ownership and due dates for every action",
            "Use GitHub and Plaky context when available",
        ],
        week_label=f"{week}-week",
        target_date_iso=target.isoformat(),
        notes="Use recurring schedule and include mandatory round table.",
    )
    out = output or _default_output(team, req.meeting_type, week)
    _generate_and_write(req, out, provider=provider, model=model)


@plan_app.command()
def custom(
    meeting_title: str = typer.Option(..., help="Meeting title"),
    meeting_type: str = typer.Option(..., help="Meeting type slug for output filename"),
    team: str = typer.Option(
        "all-teams",
        "--team",
        "-t",
        prompt="Which team is this meeting for? (ai-ml, qa, frontend-backend-infra, it, all-teams)",
    ),
    week: str = typer.Option("next", "--week", help="current or next"),
    attendees: int = typer.Option(15, min=2, max=100),
    notes: str = typer.Option("", help="Additional planning context"),
    output: Path | None = typer.Option(None, "--output", "-o"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    model: Optional[str] = typer.Option(None, "--model"),
) -> None:
    """Generate a custom meeting plan."""
    if team not in TEAM_CHOICES:
        raise typer.BadParameter(f"team must be one of: {', '.join(TEAM_CHOICES)}")
    if week not in WEEK_CHOICES:
        raise typer.BadParameter(f"week must be one of: {', '.join(WEEK_CHOICES)}")
    target = _week_anchor(week)
    req = MeetingRequest(
        meeting_title=meeting_title,
        meeting_type=meeting_type,
        team_focus=team,
        attendees_count=attendees,
        objectives=[
            "Drive clarity on immediate goals",
            "Collect concise status from each participant",
            "Turn blockers into owner-assigned action items",
        ],
        week_label=f"{week}-week",
        target_date_iso=target.isoformat(),
        notes=notes or None,
    )
    out = output or _default_output(team, meeting_type, week)
    _generate_and_write(req, out, provider=provider, model=model)
