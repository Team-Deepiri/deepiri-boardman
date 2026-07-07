import asyncio
import json
import re
from html import escape
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from boardman.agent.service import run_agent_chat
from boardman.assignment.qa_picker import ensure_github_owner_repo
from boardman.database.models import AgentSession, ProjectContext, ScanRun
from boardman.database.session import async_session
from boardman.plaky.client import PlakyClient
from boardman.plaky.inventory import collect_plaky_inventory
from boardman.plaky.placement import context_board_id, context_group_id, plaky_placement_context
from boardman.readiness import build_readiness_report
from boardman.repos_config import (
    list_registered_repos,
    list_workspace_repos,
    repos_yaml_canonical_repo_key,
    upsert_repo,
)
from boardman.services.direction_init import init_direction_file
from boardman.services.pr_link_comment import collect_pr_urls, format_pr_link_comment
from boardman.services.scan_handler import run_repo_scan
from boardman.services.task_mutations import (
    CreateSubtaskInput,
    CreateTaskInput,
    UpdateTaskInput,
    create_subtask_internal,
    create_task_internal,
    update_task_internal,
)
from boardman.settings import settings

app = typer.Typer(help="deepiri-boardman CLI")
agent_app = typer.Typer(help="AI agent and repo scan")
console = Console()


@app.command()
def create_task(
    title: str = typer.Option(..., prompt=True, help="Task title"),
    description: str = typer.Option("", "--description", "-d", help="Task description"),
    priority: str = typer.Option("medium", "--priority", "-p", help="Priority: low, medium, high"),
    status: str = typer.Option("in_progress", "--status", help="Workflow status"),
    task_type: str = typer.Option("feature", "--type", help="Task type"),
    github_repos: list[str] | None = typer.Option(
        None,
        "--github-repo",
        help="GitHub repo slug(s), repeatable or comma/space separated",
    ),
    plaky_board_id: str | None = typer.Option(None, "--board-id", help="Explicit Plaky board id"),
    plaky_group_id: str | None = typer.Option(None, "--group-id", help="Explicit Plaky group id"),
    engineer_plaky_id: str | None = typer.Option(
        None, "--engineer-id", help="Plaky user id for engineer"
    ),
    qa_plaky_id: str | None = typer.Option(None, "--qa-id", help="Plaky user id for QA"),
    auto_assign_team: bool = typer.Option(
        True,
        "--auto-assign-team/--no-auto-assign-team",
        help="Auto-pick QA from team assignments when --qa-id is not set (engineer is never auto-picked)",
    ),
):
    async def run():
        result = await create_task_internal(
            CreateTaskInput(
                title=title,
                description=description,
                priority=priority,
                status=status,
                task_type=task_type,
                github_repos=github_repos,
                plaky_board_id=plaky_board_id,
                plaky_group_id=plaky_group_id,
                engineer_plaky_id=engineer_plaky_id,
                qa_plaky_id=qa_plaky_id,
                auto_assign_team=auto_assign_team,
            )
        )
        if result.get("ok"):
            task_url = result.get("task_url")
            if not task_url:
                task = result.get("task") if isinstance(result.get("task"), dict) else {}
                task_url = task.get("url") or task.get("task_url")
            task_id = result.get("task_id") or (
                (result.get("task") or {}).get("id")
                if isinstance(result.get("task"), dict)
                else None
            )
            created_ref = task_url or (f"id={task_id}" if task_id else "(no url/id returned)")
            console.print(f"[green]Task created:[/green] {created_ref}")
            post_assign = result.get("post_create_assignment")
            if isinstance(post_assign, dict):
                if post_assign.get("ok"):
                    if not post_assign.get("skipped"):
                        source = post_assign.get("item_id_source")
                        source_msg = f" source={source}" if source else ""
                        console.print(f"[green]Field patch applied[/green]{source_msg}")
                else:
                    console.print(
                        f"[yellow]Field patch warning:[/yellow] {post_assign.get('message')}"
                    )
            warnings = result.get("tag_resolution_warnings")
            if isinstance(warnings, list) and warnings:
                console.print(
                    f"[yellow]Tag resolution warnings:[/yellow] {json.dumps(warnings, indent=2)}"
                )
            qa_pick = result.get("qa_roster_pick")
            if isinstance(qa_pick, dict):
                console.print(f"[dim]QA roster pick:[/dim] {json.dumps(qa_pick, indent=2)}")
        else:
            console.print(f"[red]Error:[/red] {result.get('message')}")
            raise typer.Exit(1)

    asyncio.run(run())


@app.command()
def create_subtask(
    parent_task_id: str = typer.Option(
        ..., "--parent-task-id", prompt=True, help="Parent Plaky task ID"
    ),
    title: str = typer.Option(..., "--title", "-t", prompt=True, help="Subtask title"),
    description: str = typer.Option("", "--description", "-d", help="Subtask description"),
    priority: str = typer.Option("medium", "--priority", "-p", help="Priority: low, medium, high"),
    status: str = typer.Option("in_progress", "--status", help="Workflow status"),
    task_type: str = typer.Option("feature", "--type", help="Task type"),
    github_repos: list[str] | None = typer.Option(
        None,
        "--github-repo",
        help="GitHub repo slug(s), repeatable or comma/space separated",
    ),
    engineer_plaky_id: str | None = typer.Option(
        None, "--engineer-id", help="Plaky user id for contributor/engineer"
    ),
    qa_plaky_id: str | None = typer.Option(None, "--qa-id", help="Plaky user id for QA"),
    auto_assign_qa: bool = typer.Option(
        True,
        "--auto-assign-qa/--no-auto-assign-qa",
        help="Auto-pick QA from team assignments when --qa-id is not set",
    ),
    plaky_board_id: str | None = typer.Option(
        None,
        "--board-id",
        help="Plaky board id used for schema/field patch resolution",
    ),
    plaky_group_id: str | None = typer.Option(
        None,
        "--group-id",
        help="Plaky group id used for subtask placement fallback",
    ),
):
    async def run():
        result = await create_subtask_internal(
            CreateSubtaskInput(
                parent_task_id=parent_task_id,
                title=title,
                description=description,
                priority=priority,
                status=status,
                task_type=task_type,
                github_repos=github_repos,
                engineer_plaky_id=engineer_plaky_id,
                qa_plaky_id=qa_plaky_id,
                auto_assign_qa=auto_assign_qa,
                plaky_board_id=plaky_board_id,
                plaky_group_id=plaky_group_id,
            )
        )
        if result.get("ok"):
            subtask = result.get("subtask") if isinstance(result.get("subtask"), dict) else {}
            subtask_ref = subtask.get("url") or subtask.get("id") or subtask.get("taskId")
            created_ref = subtask_ref or f"parent={parent_task_id}"
            console.print(f"[green]Subtask created:[/green] {created_ref}")
        else:
            console.print(f"[red]Error:[/red] {result.get('message')}")
            raise typer.Exit(1)

    asyncio.run(run())


@app.command()
def link_pr(
    pr_urls: str = typer.Option(
        ...,
        "--pr-url",
        "-u",
        prompt=True,
        help="GitHub PR URL(s): one URL, or several comma- or whitespace-separated",
    ),
    task_id: str = typer.Option(..., prompt=True, help="Plaky task ID"),
    plaky_board_id: str | None = typer.Option(
        None,
        "--board-id",
        help="Plaky board id (optional; speeds v1/public item comments).",
    ),
    update_status: bool = typer.Option(
        False, "--update-status", help="Update task status on merge"
    ),
    print_response: bool = typer.Option(
        False,
        "--print-response",
        help="Print full JSON result from Plaky (status, route, comment payload, posted text).",
    ),
):
    plaky = PlakyClient()

    async def run():
        parts = [p for p in re.split(r"[\s,]+", (pr_urls or "").strip()) if p.strip()]
        urls = collect_pr_urls(pr_url=None, pr_urls=parts or None)
        if not urls:
            console.print("[red]Error:[/red] supply at least one PR URL")
            return
        comment = format_pr_link_comment(urls)
        bid = (plaky_board_id or "").strip() or None
        result = await plaky.add_comment(task_id, comment, board_id=bid)
        if print_response:
            dbg = dict(result)
            dbg["posted_comment_text"] = comment
            console.print(json.dumps(dbg, indent=2, default=str))
        if result.get("ok"):
            console.print("[green]PR linked successfully[/green]")
            if update_status:
                await update_task_internal(
                    task_id,
                    UpdateTaskInput(status=settings.plaky_pr_merge_status),
                )
                console.print(f"[green]Status updated to {settings.plaky_pr_merge_status}[/green]")
        else:
            console.print(f"[red]Error:[/red] {result.get('message')}")

    asyncio.run(run())


@app.command("list")
def list_tasks_cmd(
    status: str = typer.Option("open", "--status", "-s", help="Task status: open, done, etc."),
    format: str = typer.Option("table", "--format", "-f", help="Output format: table, json"),
    plaky_board_id: str | None = typer.Option(
        None,
        "--board-id",
        help="Board id for listing items in v1/public mode.",
    ),
):
    plaky = PlakyClient()

    async def run():
        result = await plaky.get_tasks(status=status, board_id=plaky_board_id)
        if not result.get("ok"):
            console.print(f"[red]Error:[/red] {result.get('message')}")
            return

        tasks = result.get("tasks", [])
        if not tasks:
            msg = str(result.get("message") or "").strip()
            if msg:
                console.print(f"[yellow]{msg}[/yellow]")
        if format == "json":
            import json

            console.print(json.dumps(tasks, indent=2))
        else:
            table = Table(title=f"Plaky Tasks ({status})")
            table.add_column("ID", style="cyan")
            table.add_column("Title")
            table.add_column("Status")
            for task in tasks:
                task_id = str(task.get("id") or task.get("itemId") or task.get("taskId") or "N/A")
                title = str(task.get("title") or task.get("name") or "Untitled")
                task_status = (
                    task.get("status")
                    or task.get("state")
                    or task.get("workflowStatus")
                    or task.get("workflow_state")
                    or "unknown"
                )
                table.add_row(task_id, title, str(task_status))
            console.print(table)

    asyncio.run(run())


@app.command("update-task")
def update_task_cmd(
    task_id: str = typer.Option(..., "--task-id", prompt=True, help="Plaky task ID"),
    status: str | None = typer.Option(None, "--status", help="New status"),
    task_type: str | None = typer.Option(None, "--type", help="New task type"),
    priority: str | None = typer.Option(None, "--priority", "-p", help="New priority"),
    qa_plaky_id: str | None = typer.Option(None, "--qa-id", help="Plaky user id for QA field"),
    auto_assign_qa: bool = typer.Option(
        False,
        "--auto-assign-qa/--no-auto-assign-qa",
        help="Auto-pick QA from team assignments using --github-repo",
    ),
    github_repo: str | None = typer.Option(
        None,
        "--github-repo",
        help="GitHub repo for --auto-assign-qa (owner/repo or repo name; bare names get GITHUB_BARE_REPO_OWNER)",
    ),
    plaky_board_id: str | None = typer.Option(
        None, "--board-id", help="Explicit board id for field patch"
    ),
):
    async def run():
        result = await update_task_internal(
            task_id,
            UpdateTaskInput(
                status=status,
                task_type=task_type,
                priority=priority,
                qa_plaky_id=qa_plaky_id,
                auto_assign_qa=auto_assign_qa,
                github_repo=github_repo,
                plaky_board_id=plaky_board_id,
            ),
        )
        ops = result.get("operations")
        op_messages: list[str] = []
        if isinstance(ops, dict):
            for name, payload in ops.items():
                if isinstance(payload, dict):
                    msg = str(payload.get("message") or "").strip()
                    if msg:
                        op_messages.append(f"{name}: {msg}")

        if result.get("ok"):
            console.print("[green]Task updated[/green]")
        else:
            summary = str(result.get("message") or "").strip() or (
                op_messages[0] if op_messages else "Update failed"
            )
            console.print(f"[red]Error:[/red] {summary}")
            if len(op_messages) > 1:
                for extra in op_messages[1:]:
                    console.print(f"[yellow]- {extra}[/yellow]")
            if not op_messages and isinstance(ops, dict) and ops:
                console.print(json.dumps(ops, indent=2))
            raise typer.Exit(1)
        if isinstance(ops, dict) and ops:
            console.print(json.dumps(ops, indent=2))

    asyncio.run(run())


@app.command()
def sync(
    repo: str = typer.Option(
        ...,
        prompt=True,
        help="GitHub repo name",
    ),
    board_id: str = typer.Option(
        ...,
        "--board-id",
        help="Plaky board id",
    ),
    group_id: str = typer.Option(
        ...,
        "--group-id",
        help="Plaky group id",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be synced without making changes"
    ),
):
    if not settings.github_pat:
        console.print("[red]Error: GITHUB_PAT not configured[/red]")
        raise typer.Exit(1)
    repo = ensure_github_owner_repo(repo)

    async def run():
        async with httpx.AsyncClient() as client:
            headers = {
                "Authorization": f"Bearer {settings.github_pat}",
                "Accept": "application/vnd.github+json",
            }
            resp = await client.get(
                f"https://api.github.com/repos/{repo}/issues?state=open",
                headers=headers,
                follow_redirects=True,
            )
            if resp.status_code != 200:
                console.print(f"[red]Error fetching issues:[/red] {resp.text}")
                return

            issues = resp.json()
            console.print(f"Found {len(issues)} open issues in {repo}")

            for issue in issues:
                title = f"{repo.split('/')[-1]} Issue: {issue['title']}"
                issue_url = str(issue.get("html_url") or "").strip()
                issue_number = issue.get("number")
                issue_body = str(issue.get("body") or "").strip()
                if settings.plaky_pr_comment_links_as_html and issue_url:
                    label = (
                        f"{repo} issue #{issue_number}" if issue_number is not None else issue_url
                    )
                    issue_link = f'<a href="{escape(issue_url, quote=True)}">{escape(label)}</a>'
                else:
                    issue_link = issue_url
                body = f"{issue_body}\n\nIssue: {issue_link}" if issue_link else issue_body
                console.print(f"  - #{issue['number']}: {issue['title']}")
                if not dry_run:
                    result = await create_task_internal(
                        CreateTaskInput(
                            title=title,
                            description=body,
                            github_repos=[repo],
                            task_type="Issue",
                            status="Available",
                            priority="High",
                            plaky_board_id=board_id,
                            plaky_group_id=group_id,
                            auto_assign_team=False,
                        )
                    )
                    if result.get("ok"):
                        console.print(f"    [green]Created:[/green] {result.get('task_url')}")
                    else:
                        console.print(f"    [red]Failed:[/red] {result.get('message')}")

    asyncio.run(run())


@app.command("register")
def register_repo(
    repo: str = typer.Argument(
        ...,
        help="Repository name or owner/repo; YAML key is repo name only. Bare names prepend "
        "GITHUB_BARE_REPO_OWNER for GitHub as owner/repo elsewhere in the toolchain.",
    ),
    category: str = typer.Option(
        ..., "--category", "-c", help="ai|ml|backend|frontend|infrastructure"
    ),
    plaky_table: str = typer.Option(..., "--table", "-t", help="Plaky table name"),
    description: str = typer.Option("", "--description", "-d"),
):
    canon = repos_yaml_canonical_repo_key(repo)
    full_slug = ensure_github_owner_repo(repo)
    upsert_repo(repo, category, plaky_table, description)
    extra = ""
    if full_slug.strip() != canon.strip():
        extra = f" [dim]({full_slug})[/dim]"
    console.print(f"[green]Registered[/green] {canon}{extra} → {plaky_table}")


@app.command("scan")
def scan_repo(
    repo: str = typer.Argument(..., help="owner/repo"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    provider: str | None = typer.Option(None, "--provider"),
    model: str | None = typer.Option(None, "--model"),
):
    async def run():
        async with async_session() as session:
            result = await run_repo_scan(
                session, repo, dry_run=dry_run, provider=provider, model=model
            )
            await session.commit()
        if result.get("ok"):
            console.print(
                f"[green]OK[/green] parsed={result.get('tasks_parsed')} created={result.get('tasks_created')}"
            )
            if result.get("preview"):
                console.print(json.dumps(result["preview"], indent=2))
        else:
            console.print(f"[red]{result.get('message', result)}[/red]")
            raise typer.Exit(1)

    asyncio.run(run())


@app.command("doctor")
def doctor():
    async def run():
        ok = True
        if settings.plaky_api_key:
            console.print("[green]PLAKY_API_KEY[/green] set")
        else:
            console.print("[red]PLAKY_API_KEY[/red] missing")
            ok = False
        if settings.github_pat:
            console.print("[green]GITHUB_PAT[/green] set")
        else:
            console.print("[yellow]GITHUB_PAT[/yellow] missing (needed for scan)")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags")
                if r.status_code == 200:
                    names = [m.get("name", "") for m in r.json().get("models", [])]
                    console.print(
                        f"[green]Ollama[/green] {settings.ollama_base_url} — {len(names)} model(s)"
                    )
                    try:
                        from boardman.llm.ollama_autodetect import effective_ollama_model

                        picked = effective_ollama_model(None)
                        src = "LLM_MODEL" if (settings.llm_model or "").strip() else "auto"
                        console.print(
                            f"[dim]Boardman will use[/dim] [cyan]{picked}[/cyan] [dim]({src})[/dim]"
                        )
                    except Exception as e:
                        console.print(f"[yellow]Could not auto-pick model:[/yellow] {e}")
                    if settings.llm_model and not any(settings.llm_model in n for n in names):
                        console.print(
                            f"[yellow]LLM_MODEL[/yellow] {settings.llm_model!r} not listed in tags (pull if needed)"
                        )
                else:
                    console.print(
                        f"[yellow]Ollama[/yellow] HTTP {r.status_code} at {settings.ollama_base_url}"
                    )
        except Exception as e:
            console.print(f"[yellow]Ollama[/yellow] unreachable: {e}")
        if settings.plaky_api_key:
            plaky = PlakyClient()
            pr = await plaky.list_boards()
            if pr.get("ok"):
                n = len(pr.get("boards") or [])
                console.print(f"[green]Plaky API[/green] list_boards OK ({n} board(s))")
            else:
                console.print(
                    f"[yellow]Plaky API[/yellow] {pr.get('message', pr)} (HTTP {pr.get('status')})"
                )
        if not ok:
            console.print("[dim]Fix missing keys in .env for full functionality.[/dim]")

    asyncio.run(run())


@app.command("readiness")
def readiness_cmd(
    env_file: Path = typer.Option(
        Path(".env"),
        "--env-file",
        help="Env file to inspect without printing secret values.",
    ),
    compose_file: Path = typer.Option(
        Path("docker-compose.prod.yml"),
        "--compose-file",
        help="Docker Compose file to inspect.",
    ),
    repos_file: Path = typer.Option(Path("repos.yml"), "--repos-file", help="Repo routing YAML."),
    team_assignments_file: Path = typer.Option(
        Path("team_assignments.yml"),
        "--team-assignments-file",
        help="Plaky/team assignment YAML.",
    ),
    database_file: Path = typer.Option(
        Path("boardman.db"),
        "--database-file",
        help="SQLite file bind-mounted into Docker.",
    ),
    format: str = typer.Option("table", "--format", "-f", help="Output format: table or json."),
    strict_pending: bool = typer.Option(
        False,
        "--strict-pending",
        help="Exit non-zero when checks are pending, not only when they fail.",
    ),
):
    """Offline production readiness report for the standalone Boardman repo."""
    report = build_readiness_report(
        Path.cwd(),
        env_file=env_file,
        compose_file=compose_file,
        repos_file=repos_file,
        team_assignments_file=team_assignments_file,
        database_file=database_file,
    )

    if format == "json":
        console.print(json.dumps(report.to_dict(), indent=2))
    elif format == "table":
        status_style = {
            "pass": "green",
            "warn": "yellow",
            "pending": "magenta",
            "fail": "red",
        }
        console.print(
            "[bold]Boardman readiness[/bold] "
            f"pass={report.passed} warn={report.warnings} "
            f"pending={report.pending} fail={report.failures}"
        )
        table = Table(title="Standalone Boardman Deployment Gates")
        table.add_column("Status", width=9)
        table.add_column("Area", width=12)
        table.add_column("Check")
        table.add_column("Detail")
        table.add_column("Next")
        for check in report.checks:
            style = status_style.get(check.status, "white")
            table.add_row(
                f"[{style}]{check.status.upper()}[/{style}]",
                check.area,
                check.name,
                check.detail,
                check.next_step,
            )
        console.print(table)
    else:
        console.print("[red]Error:[/red] --format must be table or json")
        raise typer.Exit(2)

    if report.failures or (strict_pending and report.pending):
        raise typer.Exit(1)


@app.command("plaky-inventory")
def plaky_inventory_cmd(
    board_id: str | None = typer.Option(
        None,
        "--board-id",
        help="Board ID to inspect for groups, fields, and status options.",
    ),
    include_users: bool = typer.Option(
        True,
        "--include-users/--no-users",
        help="Include workspace users for member/assignee ID handoff.",
    ),
    format: str = typer.Option("table", "--format", "-f", help="Output format: table or json."),
):
    """List Plaky board/group/field/status IDs for deployment config."""

    async def run():
        inventory = await collect_plaky_inventory(
            board_id=board_id or "",
            include_users=include_users,
        )
        if format == "json":
            console.print(json.dumps(inventory, indent=2, default=str))
            return
        if format != "table":
            console.print("[red]Error:[/red] --format must be table or json")
            raise typer.Exit(2)

        _print_plaky_inventory(inventory, board_id=board_id or "")
        if not inventory.get("ok"):
            raise typer.Exit(1)

    asyncio.run(run())


def _print_plaky_inventory(inventory: dict, *, board_id: str = "") -> None:
    messages = inventory.get("messages") or []
    for message in messages:
        console.print(f"[yellow]{message}[/yellow]")

    boards = [b for b in inventory.get("boards") or [] if isinstance(b, dict)]
    board_table = Table(title="Plaky Boards")
    board_table.add_column("ID", style="cyan")
    board_table.add_column("Name")
    board_table.add_column("Space ID", style="dim")
    for board in boards:
        board_table.add_row(
            str(board.get("id") or ""),
            str(board.get("name") or ""),
            str(board.get("space_id") or ""),
        )
    console.print(board_table)

    if not board_id:
        console.print(
            "[dim]Run with --board-id <id> to list groups, fields, and status options.[/dim]"
        )

    groups = [g for g in inventory.get("groups") or [] if isinstance(g, dict)]
    if groups:
        group_table = Table(title="Plaky Groups")
        group_table.add_column("ID", style="cyan")
        group_table.add_column("Name")
        for group in groups:
            group_table.add_row(str(group.get("id") or ""), str(group.get("name") or ""))
        console.print(group_table)

    fields = [f for f in inventory.get("fields") or [] if isinstance(f, dict)]
    if fields:
        field_table = Table(title="Plaky Fields")
        field_table.add_column("Key", style="cyan")
        field_table.add_column("Name")
        field_table.add_column("Type")
        field_table.add_column("Options")
        for field in fields:
            options = field.get("options") or []
            option_labels = [
                str(opt.get("name") or opt.get("id") or opt.get("optionId") or "")
                for opt in options
                if isinstance(opt, dict)
            ]
            field_table.add_row(
                str(field.get("key") or ""),
                str(field.get("name") or ""),
                str(field.get("type") or ""),
                ", ".join([label for label in option_labels if label][:20]),
            )
        console.print(field_table)

    status_fields = [f for f in inventory.get("status_fields") or [] if isinstance(f, dict)]
    if status_fields:
        status_table = Table(title="Status Options")
        status_table.add_column("Field Key", style="cyan")
        status_table.add_column("Field Name")
        status_table.add_column("Option ID / Value")
        status_table.add_column("Option Name")
        for field in status_fields:
            for opt in field.get("options") or []:
                if not isinstance(opt, dict):
                    continue
                status_table.add_row(
                    str(field.get("key") or ""),
                    str(field.get("name") or ""),
                    str(opt.get("id") or opt.get("optionId") or opt.get("value") or ""),
                    str(opt.get("name") or opt.get("label") or opt.get("title") or ""),
                )
        console.print(status_table)

    users = [u for u in inventory.get("users") or [] if isinstance(u, dict)]
    if users:
        user_table = Table(title="Plaky Users")
        user_table.add_column("ID", style="cyan")
        user_table.add_column("Name")
        user_table.add_column("Email", style="dim")
        user_table.add_column("GitHub", style="dim")
        for user in users:
            user_table.add_row(
                str(user.get("id") or ""),
                str(user.get("name") or ""),
                str(user.get("email") or user.get("primaryEmail") or ""),
                str(user.get("github_login") or ""),
            )
        console.print(user_table)


def _agent_chat_async(
    message: str,
    session_id: str | None,
    repo: str | None,
    provider: str | None,
    model: str | None,
    allow_writes: bool,
    use_tools: bool,
) -> None:
    async def run():
        async with async_session() as session:
            async with plaky_placement_context(None, None):
                reply, sid = await run_agent_chat(
                    session,
                    message=message,
                    session_id=session_id,
                    repo=repo,
                    provider=provider,
                    model=model,
                    allow_writes=allow_writes,
                    use_tools=use_tools,
                    plaky_board_id=context_board_id(),
                    plaky_group_id=context_group_id(),
                )
            await session.commit()
        console.print(reply)
        console.print(f"[dim]session_id={sid}[/dim]")

    asyncio.run(run())


@agent_app.command("chat")
def agent_chat_cmd(
    message: str = typer.Option(..., "--message", "-m"),
    session_id: str | None = typer.Option(None, "--session"),
    repo: str | None = typer.Option(None, "--repo", "-r"),
    provider: str | None = typer.Option(None, "--provider"),
    model: str | None = typer.Option(None, "--model"),
    allow_writes: bool = typer.Option(
        False, "--allow-writes", help="Enable Plaky create/update tools"
    ),
    use_tools: bool = typer.Option(
        False,
        "--use-tools",
        help="LangChain multi-step tool agent (slower; requires AGENT_LANGCHAIN_TOOLS)",
    ),
):
    _agent_chat_async(message, session_id, repo, provider, model, allow_writes, use_tools)


@agent_app.command("ask")
def agent_ask_cmd(
    message: str = typer.Option(..., "--message", "-m"),
    session_id: str | None = typer.Option(None, "--session"),
    repo: str | None = typer.Option(None, "--repo", "-r"),
    provider: str | None = typer.Option(None, "--provider"),
    model: str | None = typer.Option(None, "--model"),
    allow_writes: bool = typer.Option(False, "--allow-writes"),
    use_tools: bool = typer.Option(False, "--use-tools"),
):
    """Alias for `boardman agent chat`."""
    _agent_chat_async(message, session_id, repo, provider, model, allow_writes, use_tools)


app.add_typer(agent_app, name="agent")


@app.command("init")
def init_direction(
    repo: str = typer.Argument(
        ...,
        help="repo name (deepiri-demo) or full slug (owner/deepiri-demo)",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing DIRECTION.md"),
):
    async def run():
        default_owner = "Team-Deepiri"
        parts = (repo or "").strip().split("/")
        if len(parts) == 1 and parts[0]:
            owner, name = default_owner, parts[0]
        elif len(parts) == 2:
            owner, name = parts[0], parts[1]
        else:
            console.print("[red]repo must be owner/name or bare repo name[/red]")
            raise typer.Exit(1)

        r = await init_direction_file(owner, name, force=force)
        if r.get("ok"):
            if r.get("skipped"):
                console.print(f"[yellow]Skipped:[/yellow] {r.get('message')} {r.get('url', '')}")
            else:
                console.print(
                    f"[green]PR created for DIRECTION.md[/green] "
                    f"base={r.get('branch')} head={r.get('pr_branch')} url={r.get('url')}"
                )
        else:
            console.print(f"[red]{r.get('message')}[/red]")
            raise typer.Exit(1)

    asyncio.run(run())


@app.command("status")
def status_cmd(
    repo: str | None = typer.Option(
        None, "--repo", help="Filter Plaky titles containing this slug"
    ),
):
    async def run():
        async with async_session() as session:
            n_scan = await session.scalar(select(func.count()).select_from(ScanRun))
            n_sess = await session.scalar(select(func.count()).select_from(AgentSession))
            n_ctx = await session.scalar(select(func.count()).select_from(ProjectContext))
        console.print(f"Scan runs: {n_scan} | Agent sessions: {n_sess} | Project contexts: {n_ctx}")
        reg = list_registered_repos()
        if reg:
            console.print(f"repos.yml entries ({len(reg)}): {', '.join(reg.keys())}")
        if settings.github_pat:
            ws = await list_workspace_repos()
            console.print(f"Workspace repos (GitHub org {settings.github_org}): {len(ws)}")
        else:
            console.print("[dim]Set GITHUB_PAT to list org repos from the GitHub API.[/dim]")
        plaky = PlakyClient()
        res = await plaky.get_tasks(status="open")
        if res.get("ok"):
            tasks = res.get("tasks") or []
            if repo:
                tasks = [t for t in tasks if repo in (t.get("title") or "")]
            console.print(f"Plaky open tasks (filtered): {len(tasks)}")
        else:
            console.print(f"[yellow]Plaky:[/yellow] {res.get('message')}")

    asyncio.run(run())


@app.command("scan-all")
def scan_all_repos(
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run", help="Default dry-run for safety"),
    provider: str | None = typer.Option(None, "--provider"),
    model: str | None = typer.Option(None, "--model"),
):
    async def run():
        reg = await list_workspace_repos()
        if not reg:
            console.print(
                "[yellow]No repos to scan — set GITHUB_PAT to discover org repos, "
                "or add entries with boardman register[/yellow]"
            )
            raise typer.Exit(0)

        for key in reg:
            console.print(f"[bold]Scanning[/bold] {key} …")
            async with async_session() as session:
                result = await run_repo_scan(
                    session, key, dry_run=dry_run, provider=provider, model=model
                )
                await session.commit()
            if result.get("ok"):
                console.print(
                    f"  [green]ok[/green] created={result.get('tasks_created')} parsed={result.get('tasks_parsed')}"
                )
            else:
                console.print(f"  [red]{result.get('message')}[/red]")

    asyncio.run(run())


if __name__ == "__main__":
    app()
