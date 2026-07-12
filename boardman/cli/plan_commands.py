from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.status import Status
from rich.table import Table

from boardman.planning.huddle.models import MeetingRequest
from boardman.planning.huddle.team_plaky_boards import boards_for_team
from boardman.planning.huddle.team_repos import TEAM_CHOICES, repos_for_team
from boardman.planning.service import (
    WEEK_CHOICES,
    default_plan_output_path,
    generate_plan,
    week_anchor,
)
from boardman.planning.team_config import resolve_planning_mappings

plan_app = typer.Typer(help="Generate weekly meeting plans (from deepiri-huddle)")
console = Console()


def _generate_and_write(
    request: MeetingRequest,
    output: Path,
    *,
    provider: str | None = None,
    model: str | None = None,
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
    provider: str | None = typer.Option(None, "--provider", help="LLM provider override"),
    model: str | None = typer.Option(None, "--model", help="LLM model override"),
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
    provider: str | None = typer.Option(None, "--provider"),
    model: str | None = typer.Option(None, "--model"),
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


@plan_app.command("doctor")
def doctor() -> None:
    """Print resolved team → GitHub repo and Plaky board mappings."""
    report = resolve_planning_mappings()
    console.print("[bold]Meeting plan configuration[/bold]")
    console.print(f"Team repos source: [cyan]{report.team_repos_source}[/cyan]")
    console.print(f"Team boards source: [cyan]{report.team_boards_source}[/cyan]")
    console.print(f"repos.yml: {report.repos_yml_path}")
    console.print(f"team_repos.json: {report.team_repos_file}")
    console.print(f"team_plaky_boards.json: {report.team_boards_file}")
    console.print()

    repo_table = Table(title="Team → GitHub repos")
    repo_table.add_column("Team")
    repo_table.add_column("Repos")
    for team in TEAM_CHOICES:
        repos = repos_for_team(report.team_repos, team)
        repo_table.add_row(team, ", ".join(repos) if repos else "(none)")
    console.print(repo_table)
    console.print()

    board_table = Table(title="Team → Plaky boards")
    board_table.add_column("Team")
    board_table.add_column("Board IDs")
    for team in TEAM_CHOICES:
        boards = boards_for_team(report.team_boards, team)
        labels = ", ".join(board.board_id for board in boards) if boards else "(none)"
        board_table.add_row(team, labels)
    console.print(board_table)
