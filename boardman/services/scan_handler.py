"""AI scan: DIRECTION.md + GitHub context → suggested Plaky tasks."""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional

import httpx

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import ProjectContext, ScanRun
from boardman.llm.completion import chat_complete, parse_json_tasks
from boardman.plaky.client import PlakyClient
from boardman.repos_config import get_routing
from boardman.settings import settings


async def _github_get(client: httpx.AsyncClient, path: str) -> Any:
    headers = {"Authorization": f"Bearer {settings.github_pat}", "Accept": "application/vnd.github+json"}
    r = await client.get(f"https://api.github.com{path}", headers=headers)
    return r


async def fetch_direction_md(client: httpx.AsyncClient, owner: str, repo: str) -> str:
    r = await _github_get(client, f"/repos/{owner}/{repo}/contents/DIRECTION.md?ref=main")
    if r.status_code == 404:
        r = await _github_get(client, f"/repos/{owner}/{repo}/contents/DIRECTION.md?ref=master")
    if r.status_code != 200:
        return f"(No DIRECTION.md found or inaccessible: HTTP {r.status_code})"
    data = r.json()
    if isinstance(data, dict) and data.get("encoding") == "base64" and data.get("content"):
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    if isinstance(data, dict) and data.get("message"):
        return f"(GitHub: {data.get('message')})"
    return "(Could not decode DIRECTION.md)"


async def fetch_recent_commits(client: httpx.AsyncClient, owner: str, repo: str, limit: int = 20) -> str:
    r = await _github_get(client, f"/repos/{owner}/{repo}/commits?per_page={limit}")
    if r.status_code != 200:
        return f"(commits unavailable: {r.status_code})"
    commits = r.json()
    lines: List[str] = []
    for c in commits[:limit]:
        sha = (c.get("sha") or "")[:7]
        msg = (c.get("commit") or {}).get("message", "").split("\n")[0]
        lines.append(f"- {sha} {msg}")
    return "\n".join(lines) if lines else "(no commits)"


async def fetch_open_issues(client: httpx.AsyncClient, owner: str, repo: str) -> str:
    r = await _github_get(client, f"/repos/{owner}/{repo}/issues?state=open&per_page=50")
    if r.status_code != 200:
        return f"(issues unavailable: {r.status_code})"
    issues = r.json()
    lines: List[str] = []
    for i in issues:
        if "pull_request" in i:
            continue
        lines.append(f"- #{i['number']}: {i.get('title', '')}")
    return "\n".join(lines) if lines else "(no open issues)"


async def fetch_plaky_titles_for_repo(repo_full: str, short: str) -> str:
    plaky = PlakyClient()
    r = await plaky.get_tasks(status="open")
    if not r.get("ok"):
        return f"(Plaky: {r.get('message')})"
    tasks = r.get("tasks") or []
    tag = short
    lines: List[str] = []
    for t in tasks:
        title = t.get("title") or ""
        if tag in title or repo_full.split("/")[-1] in title:
            lines.append(f"- {title[:120]}")
    return "\n".join(lines[:40]) if lines else "(no open Plaky tasks matched this repo tag)"


def _scan_prompt(
    repo_full: str,
    category: str,
    direction: str,
    commits: str,
    issues: str,
    plaky: str,
) -> str:
    return f"""You are a software project manager. Propose concrete Plaky tasks for this GitHub repo.

REPO: {repo_full}
CATEGORY (from repos.yml if any): {category or "unknown"}

DIRECTION.md:
{direction}

RECENT COMMITS:
{commits}

OPEN GITHUB ISSUES:
{issues}

EXISTING PLAKY TASKS (likely this repo):
{plaky}

Return ONLY a JSON array of 3-8 objects, no markdown fences:
[
  {{"title": "short title", "description": "markdown body", "priority": "low|medium|high"}},
  ...
]

Rules:
- Tasks must be actionable and specific.
- Do not duplicate items already listed as open issues or existing Plaky lines.
- Align with DIRECTION.md when present.
"""


async def run_repo_scan(
    session: AsyncSession,
    repo_full: str,
    *,
    dry_run: bool,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    if not settings.github_pat:
        return {"ok": False, "message": "GITHUB_PAT not configured"}

    parts = repo_full.split("/")
    if len(parts) != 2:
        return {"ok": False, "message": "repo must be owner/name"}
    owner, repo = parts[0], parts[1]
    short = repo
    routing = get_routing(repo_full, short, settings.github_org)

    prov = (provider or settings.llm_provider or "ollama").lower()
    if prov in ("claude",):
        prov = "anthropic"
    mdl = model or settings.llm_model

    scan_row = ScanRun(
        github_repo=repo_full,
        provider=prov,
        model=mdl,
        dry_run=dry_run,
        tasks_created=0,
    )
    session.add(scan_row)
    await session.flush()

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            direction = await fetch_direction_md(client, owner, repo)
            commits = await fetch_recent_commits(client, owner, repo)
            issues = await fetch_open_issues(client, owner, repo)
            plaky_lines = await fetch_plaky_titles_for_repo(repo_full, short)

        prompt = _scan_prompt(
            repo_full,
            routing.category if routing else "",
            direction,
            commits,
            issues,
            plaky_lines,
        )
        raw = await chat_complete(
            [{"role": "user", "content": prompt}],
            provider=prov,
            model=mdl,
            timeout=180.0,
        )
        tasks = parse_json_tasks(raw)
        if not isinstance(tasks, list):
            raise ValueError("Expected JSON array")

        scan_row.tasks_proposed = json.dumps(tasks)[:65000]

        created = 0
        plaky = PlakyClient()
        cat = routing.plaky_table if routing else ""
        routing_note = f"\n\n**Plaky routing:** `{cat}`\n**Repo:** {repo_full}\n" if cat else f"\n\n**Repo:** {repo_full}\n"

        for item in tasks:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "Task")).strip()
            desc = str(item.get("description", "")).strip()
            pri = str(item.get("priority", "medium")).lower()
            if pri not in ("low", "medium", "high"):
                pri = "medium"
            full_title = f"[{short}] {title}"
            body = desc + routing_note
            if dry_run:
                continue
            res = await plaky.create_task(title=full_title, description=body, priority=pri)
            if res.get("ok"):
                created += 1

        scan_row.tasks_created = created
        await session.flush()

        q = select(ProjectContext).where(ProjectContext.repo == repo_full)
        pc = (await session.execute(q)).scalar_one_or_none()
        summary = direction[:12000] if isinstance(direction, str) else ""
        goals = json.dumps({"last_scan_id": scan_row.id, "tasks_parsed": len(tasks), "tasks_created": created})
        if pc is None:
            session.add(
                ProjectContext(
                    repo=repo_full,
                    summary=summary,
                    goals_json=goals,
                    last_scanned=datetime.utcnow(),
                )
            )
        else:
            pc.summary = summary
            pc.goals_json = goals
            pc.last_scanned = datetime.utcnow()
        await session.flush()

        cap = tasks[:30] if isinstance(tasks, list) else []
        return {
            "ok": True,
            "dry_run": dry_run,
            "tasks_parsed": len(tasks),
            "tasks_created": created if not dry_run else 0,
            "preview": cap,
            "scan_id": scan_row.id,
        }
    except Exception as e:
        scan_row.error = str(e)[:2000]
        await session.flush()
        return {"ok": False, "message": str(e), "scan_id": scan_row.id}
