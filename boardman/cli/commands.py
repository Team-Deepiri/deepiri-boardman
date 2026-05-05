import asyncio
import json
import re
from typing import List, Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table

from sqlalchemy import func, select

from boardman.agent.service import run_agent_chat
from boardman.plaky.placement import context_board_id, context_group_id, plaky_placement_context
from boardman.database.models import AgentSession, ProjectContext, ScanRun
from boardman.database.session import async_session
from boardman.plaky.client import PlakyClient
from boardman.services.pr_link_comment import collect_pr_urls, format_pr_link_comment
from boardman.services.task_mutations import (
    CreateTaskInput,
    UpdateTaskInput,
    create_task_internal,
    update_task_internal,
)
from boardman.repos_config import list_registered_repos, list_workspace_repos, upsert_repo
from boardman.services.direction_init import init_direction_file
from boardman.services.scan_handler import run_repo_scan
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
    github_repos: Optional[List[str]] = typer.Option(
        None,
        "--github-repo",
        help="GitHub repo slug(s), repeatable or comma/space separated",
    ),
    plaky_board_id: Optional[str] = typer.Option(None, "--board-id", help="Explicit Plaky board id"),
    plaky_group_id: Optional[str] = typer.Option(None, "--group-id", help="Explicit Plaky group id"),
    engineer_plaky_id: Optional[str] = typer.Option(None, "--engineer-id", help="Plaky user id for engineer"),
    qa_plaky_id: Optional[str] = typer.Option(None, "--qa-id", help="Plaky user id for QA"),
    auto_assign_team: bool = typer.Option(
        True,
        "--auto-assign-team/--no-auto-assign-team",
        help="Auto-pick engineer/QA from team assignments when ids are not provided",
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
            task_id = (
                result.get("task_id")
                or ((result.get("task") or {}).get("id") if isinstance(result.get("task"), dict) else None)
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
                    console.print(f"[yellow]Field patch warning:[/yellow] {post_assign.get('message')}")
            warnings = result.get("tag_resolution_warnings")
            if isinstance(warnings, list) and warnings:
                console.print(f"[yellow]Tag resolution warnings:[/yellow] {json.dumps(warnings, indent=2)}")
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
    update_status: bool = typer.Option(False, "--update-status", help="Update task status on merge"),
):
    plaky = PlakyClient()

    async def run():
        parts = [p for p in re.split(r"[\s,]+", (pr_urls or "").strip()) if p.strip()]
        urls = collect_pr_urls(pr_url=None, pr_urls=parts or None)
        if not urls:
            console.print("[red]Error:[/red] supply at least one PR URL")
            return
        comment = format_pr_link_comment(urls)
        result = await plaky.add_comment(task_id, comment)
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
):
    plaky = PlakyClient()

    async def run():
        result = await plaky.get_tasks(status=status)
        if not result.get("ok"):
            console.print(f"[red]Error:[/red] {result.get('message')}")
            return

        tasks = result.get("tasks", [])
        if format == "json":
            import json
            console.print(json.dumps(tasks, indent=2))
        else:
            table = Table(title=f"Plaky Tasks ({status})")
            table.add_column("ID", style="cyan")
            table.add_column("Title")
            table.add_column("Status")
            for task in tasks:
                task_id = task.get("id") or task.get("taskId", "N/A")
                title = task.get("title", "Untitled")
                task_status = task.get("status", "unknown")
                table.add_row(task_id, title, task_status)
            console.print(table)

    asyncio.run(run())


@app.command("update-task")
def update_task_cmd(
    task_id: str = typer.Option(..., "--task-id", prompt=True, help="Plaky task ID"),
    status: Optional[str] = typer.Option(None, "--status", help="New status"),
    task_type: Optional[str] = typer.Option(None, "--type", help="New task type"),
    priority: Optional[str] = typer.Option(None, "--priority", "-p", help="New priority"),
    qa_plaky_id: Optional[str] = typer.Option(None, "--qa-id", help="Plaky user id for QA field"),
    auto_assign_qa: bool = typer.Option(
        False,
        "--auto-assign-qa/--no-auto-assign-qa",
        help="Auto-pick QA from team assignments using --github-repo",
    ),
    github_repo: Optional[str] = typer.Option(
        None,
        "--github-repo",
        help="owner/repo used when --auto-assign-qa is enabled",
    ),
    plaky_board_id: Optional[str] = typer.Option(None, "--board-id", help="Explicit board id for field patch"),
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
            summary = str(result.get("message") or "").strip() or (op_messages[0] if op_messages else "Update failed")
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
    repo: str = typer.Option(..., prompt=True, help="GitHub repo (owner/repo)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be synced without making changes"),
):
    if not settings.github_pat:
        console.print("[red]Error: GITHUB_PAT not configured[/red]")
        raise typer.Exit(1)

    async def run():
        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"Bearer {settings.github_pat}", "Accept": "application/vnd.github+json"}
            resp = await client.get(f"https://api.github.com/repos/{repo}/issues?state=open", headers=headers)
            if resp.status_code != 200:
                console.print(f"[red]Error fetching issues:[/red] {resp.text}")
                return

            issues = resp.json()
            console.print(f"Found {len(issues)} open issues in {repo}")

            plaky = PlakyClient()
            for issue in issues:
                title = f"[{repo}] {issue['title']}"
                body = f"{issue.get('body', '')}\n\n{issue['html_url']}"
                console.print(f"  - #{issue['number']}: {issue['title']}")
                if not dry_run:
                    result = await create_task_internal(
                        CreateTaskInput(
                            title=title,
                            description=body,
                            priority="medium",
                            repo=repo,
                            github_repos=[repo],
                        )
                    )
                    if result.get("ok"):
                        console.print(f"    [green]Created:[/green] {result.get('task_url')}")
                    else:
                        console.print(f"    [red]Failed:[/red] {result.get('message')}")

    asyncio.run(run())


@app.command("register")
def register_repo(
    repo: str = typer.Argument(..., help="GitHub repo owner/name"),
    category: str = typer.Option(..., "--category", "-c", help="ai|ml|backend|frontend|infrastructure"),
    plaky_table: str = typer.Option(..., "--table", "-t", help="Plaky table name"),
    description: str = typer.Option("", "--description", "-d"),
):
    upsert_repo(repo, category, plaky_table, description)
    console.print(f"[green]Registered[/green] {repo} → {plaky_table}")


@app.command("scan")
def scan_repo(
    repo: str = typer.Argument(..., help="owner/repo"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    model: Optional[str] = typer.Option(None, "--model"),
):
    async def run():
        async with async_session() as session:
            result = await run_repo_scan(session, repo, dry_run=dry_run, provider=provider, model=model)
            await session.commit()
        if result.get("ok"):
            console.print(f"[green]OK[/green] parsed={result.get('tasks_parsed')} created={result.get('tasks_created')}")
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
                    console.print(f"[green]Ollama[/green] {settings.ollama_base_url} — {len(names)} model(s)")
                    try:
                        from boardman.llm.ollama_autodetect import effective_ollama_model

                        picked = effective_ollama_model(None)
                        src = "LLM_MODEL" if (settings.llm_model or "").strip() else "auto"
                        console.print(f"[dim]Boardman will use[/dim] [cyan]{picked}[/cyan] [dim]({src})[/dim]")
                    except Exception as e:
                        console.print(f"[yellow]Could not auto-pick model:[/yellow] {e}")
                    if settings.llm_model and not any(settings.llm_model in n for n in names):
                        console.print(f"[yellow]LLM_MODEL[/yellow] {settings.llm_model!r} not listed in tags (pull if needed)")
                else:
                    console.print(f"[yellow]Ollama[/yellow] HTTP {r.status_code} at {settings.ollama_base_url}")
        except Exception as e:
            console.print(f"[yellow]Ollama[/yellow] unreachable: {e}")
        if settings.plaky_api_key:
            plaky = PlakyClient()
            pr = await plaky.list_boards()
            if pr.get("ok"):
                n = len(pr.get("boards") or [])
                console.print(f"[green]Plaky API[/green] list_boards OK ({n} board(s))")
            else:
                console.print(f"[yellow]Plaky API[/yellow] {pr.get('message', pr)} (HTTP {pr.get('status')})")
        if not ok:
            console.print("[dim]Fix missing keys in .env for full functionality.[/dim]")

    asyncio.run(run())


def _agent_chat_async(
    message: str,
    session_id: Optional[str],
    repo: Optional[str],
    provider: Optional[str],
    model: Optional[str],
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
    session_id: Optional[str] = typer.Option(None, "--session"),
    repo: Optional[str] = typer.Option(None, "--repo", "-r"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    model: Optional[str] = typer.Option(None, "--model"),
    allow_writes: bool = typer.Option(False, "--allow-writes", help="Enable Plaky create/update tools"),
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
    session_id: Optional[str] = typer.Option(None, "--session"),
    repo: Optional[str] = typer.Option(None, "--repo", "-r"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    model: Optional[str] = typer.Option(None, "--model"),
    allow_writes: bool = typer.Option(False, "--allow-writes"),
    use_tools: bool = typer.Option(False, "--use-tools"),
):
    """Alias for `boardman agent chat`."""
    _agent_chat_async(message, session_id, repo, provider, model, allow_writes, use_tools)


app.add_typer(agent_app, name="agent")


@app.command("init")
def init_direction(
    repo: str = typer.Argument(..., help="owner/repo"),
    branch: Optional[str] = typer.Option(None, "--branch"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing DIRECTION.md"),
):
    parts = repo.split("/")
    if len(parts) != 2:
        console.print("[red]repo must be owner/name[/red]")
        raise typer.Exit(1)

    async def run():
        r = await init_direction_file(parts[0], parts[1], branch=branch, force=force)
        if r.get("ok"):
            if r.get("skipped"):
                console.print(f"[yellow]Skipped:[/yellow] {r.get('message')} {r.get('url', '')}")
            else:
                console.print(f"[green]DIRECTION.md created[/green] branch={r.get('branch')} url={r.get('url')}")
        else:
            console.print(f"[red]{r.get('message')}[/red]")
            raise typer.Exit(1)

    asyncio.run(run())


@app.command("status")
def status_cmd(
    repo: Optional[str] = typer.Option(None, "--repo", help="Filter Plaky titles containing this slug"),
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
    provider: Optional[str] = typer.Option(None, "--provider"),
    model: Optional[str] = typer.Option(None, "--model"),
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
                result = await run_repo_scan(session, key, dry_run=dry_run, provider=provider, model=model)
                await session.commit()
            if result.get("ok"):
                console.print(f"  [green]ok[/green] created={result.get('tasks_created')} parsed={result.get('tasks_parsed')}")
            else:
                console.print(f"  [red]{result.get('message')}[/red]")

    asyncio.run(run())


if __name__ == "__main__":
    app()