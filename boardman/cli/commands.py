import asyncio
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table

from boardman.plaky.client import PlakyClient
from boardman.settings import settings

app = typer.Typer(help="deepiri-boardman CLI")
console = Console()


@app.command()
def create_task(
    title: str = typer.Option(..., prompt=True, help="Task title"),
    description: str = typer.Option("", "--description", "-d", help="Task description"),
    priority: str = typer.Option("medium", "--priority", "-p", help="Priority: low, medium, high"),
    repo: Optional[str] = typer.Option(None, "--repo", "-r", help="Repository name for tag"),
):
    plaky = PlakyClient()
    full_title = f"[{repo}] {title}" if repo else title

    async def run():
        result = await plaky.create_task(title=full_title, description=description, priority=priority)
        if result.get("ok"):
            console.print(f"[green]Task created:[/green] {result.get('task_url')}")
        else:
            console.print(f"[red]Error:[/red] {result.get('message')}")

    asyncio.run(run())


@app.command()
def link_pr(
    pr_url: str = typer.Option(..., prompt=True, help="GitHub PR URL"),
    task_id: str = typer.Option(..., prompt=True, help="Plaky task ID"),
    update_status: bool = typer.Option(False, "--update-status", help="Update task status on merge"),
):
    plaky = PlakyClient()

    async def run():
        comment = f"**PR Linked:** [View PR]({pr_url})"
        result = await plaky.add_comment(task_id, comment)
        if result.get("ok"):
            console.print("[green]PR linked successfully[/green]")
            if update_status:
                await plaky.update_task_status(task_id, settings.plaky_pr_merge_status)
                console.print(f"[green]Status updated to {settings.plaky_pr_merge_status}[/green]")
        else:
            console.print(f"[red]Error:[/red] {result.get('message')}")

    asyncio.run(run())


@app.command()
def list(
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
                    result = await plaky.create_task(title=title, description=body, priority="medium")
                    if result.get("ok"):
                        console.print(f"    [green]Created:[/green] {result.get('task_url')}")
                    else:
                        console.print(f"    [red]Failed:[/red] {result.get('message')}")

    asyncio.run(run())


if __name__ == "__main__":
    app()