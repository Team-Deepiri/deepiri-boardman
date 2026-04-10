"""AI scan: DIRECTION.md + GitHub context → suggested Plaky tasks."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import ProjectContext, ScanRun
from boardman.assignment.qa_picker import build_assignment_field_map
from boardman.github.repo_fetch import (
    fetch_direction_md,
    fetch_open_issues,
    fetch_recent_commits,
)
from boardman.llm.completion import chat_complete, parse_json_tasks
from boardman.llm.ollama_autodetect import effective_ollama_model
from boardman.plaky.client import PlakyClient
from boardman.plaky.hierarchy import effective_plaky_placement
from boardman.repos_config import get_routing
from boardman.settings import settings


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

Return ONLY a JSON array of 3-18 objects, no markdown fences:
[
  {{"title": "short title", "description": "markdown body", "priority": "low|medium|high", "fields": {{}}}},
  ...
]

Each object may include optional **fields**: a JSON object mapping Plaky **item field keys** (from your board schema / API, e.g. status or custom column keys) to values Plaky accepts (exact option label, option id, assignee id, etc.). Omit **fields** entirely if you are unsure — items will still be created in the target group.

Rules:
- Tasks must be actionable and specific; scale count to initiative size (small fix = few tasks, large goal = more).
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
    if prov == "ollama":
        mdl = effective_ollama_model(model)
    else:
        mdl = (model or settings.llm_model or "").strip()
        if prov == "anthropic":
            mdl = mdl or "claude-sonnet-4-20250514"
        elif prov in ("openai", "gpt"):
            mdl = mdl or "gpt-4o-mini"
        elif prov in ("gemini", "google"):
            mdl = mdl or "gemini-2.0-flash"

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
        routing_note = f"\n\n**Plaky group (label):** `{cat}`\n**Repo:** {repo_full}\n" if cat else f"\n\n**Repo:** {repo_full}\n"
        bid, gid = effective_plaky_placement(routing)
        default_assign = build_assignment_field_map(repo_full)

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
            field_map: Dict[str, Any] = dict(default_assign)
            raw_fields = item.get("fields")
            if isinstance(raw_fields, dict):
                field_map.update({str(k): v for k, v in raw_fields.items() if str(k).strip()})
            if dry_run:
                continue
            res = await plaky.create_task(
                title=full_title,
                description=body,
                priority=pri,
                board_id=bid,
                group_id=gid,
                field_values=field_map if field_map else None,
            )
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
