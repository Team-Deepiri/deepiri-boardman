from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.status import Status

from boardman.planning.models import MeetingRequest
from boardman.planning.service import (
    WEEK_CHOICES,
    default_plan_output_path,
    generate_plan,
    week_anchor,
)
from boardman.planning.team_repos import TEAM_CHOICES

plan_app = typer.Typer(help="Generate weekly meeting plans (from deepiri-huddle)")
console = Console()


def _generate_and_write(
    request: MeetingRequest,
    output: Path,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> None:
    with Status("Generating meeting plan...", spinner="dots", console=console):
        plan = generate_plan(
            request,
            output_path=output,
            provider=provider,
            model=model,
        )
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
    target = week_anchor(week)
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
    out = output or default_plan_output_path(team, req.meeting_type, week)
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
    target = week_anchor(week)
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
    out = output or default_plan_output_path(team, meeting_type, week)
    _generate_and_write(req, out, provider=provider, model=model)
