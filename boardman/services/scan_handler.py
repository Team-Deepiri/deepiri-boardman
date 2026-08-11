"""AI scan: DIRECTION.md + GitHub context → suggested Plaky tasks."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.agent.task_draft import normalize_task_title
from boardman.assignment.qa_picker import build_assignment_field_map
from boardman.database.models import ProjectContext, ScanRun
from boardman.github.repo_fetch import (
    fetch_direction_md,
    fetch_open_issues,
    fetch_recent_commits,
)
from boardman.llm.completion import chat_complete, parse_json_tasks
from boardman.llm.ollama_autodetect import effective_ollama_model
from boardman.plaky.client import PlakyClient
from boardman.plaky.hierarchy import effective_plaky_placement
from boardman.repos_config import get_routing_async
from boardman.settings import settings


def _normalize_task_fields(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in raw.items():
        ks = str(k).strip()
        if not ks:
            continue
        out[ks] = v
    return out


def _normalize_scan_tasks(raw_tasks: Any) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    if not isinstance(raw_tasks, list):
        return [], ["Model output was not a JSON array; no tasks parsed."]
    out: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for i, item in enumerate(raw_tasks):
        if not isinstance(item, dict):
            warnings.append(f"Skipped non-object item at index {i}.")
            continue
        title_raw = str(item.get("title") or "").strip()
        title, title_err = normalize_task_title(title_raw, mode="truncate")
        if title_err:
            warnings.append(f"Skipped item {i} with empty title.")
            continue
        key = title.casefold()
        if key in seen_titles:
            warnings.append(f"Skipped duplicate title: {title!r}.")
            continue
        seen_titles.add(key)

        desc = str(item.get("description") or "").strip()
        pri = str(item.get("priority") or "medium").strip().lower()
        if pri not in ("low", "medium", "high"):
            warnings.append(f"Normalized invalid priority {pri!r} to 'medium' for title {title!r}.")
            pri = "medium"
        fields = _normalize_task_fields(item.get("fields"))
        evidence = item.get("evidence")
        assumptions = item.get("assumptions")
        unknowns = item.get("unknowns")
        out.append(
            {
                "title": title[:160],
                "description": desc[:8000],
                "priority": pri,
                "fields": fields,
                "evidence": evidence if isinstance(evidence, list) else [],
                "assumptions": assumptions if isinstance(assumptions, list) else [],
                "unknowns": unknowns if isinstance(unknowns, list) else [],
            }
        )
    if not out:
        warnings.append("No valid task objects remained after normalization.")
    return out[:30], warnings


async def fetch_plaky_titles_for_repo(repo_full: str, short: str) -> str:
    plaky = PlakyClient()
    r = await plaky.get_tasks(status="open")
    if not r.get("ok"):
        return f"(Plaky: {r.get('message')})"
    tasks = r.get("tasks") or []
    tag = short
    lines: list[str] = []
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
  {{
    "title": "short title",
    "description": "markdown body",
    "priority": "low|medium|high",
    "fields": {{}},
    "evidence": ["facts from commits/issues/direction that justify this task"],
    "assumptions": ["assumption made for planning"],
    "unknowns": ["missing info to confirm before implementation"]
  }},
  ...
]

Each object may include optional **fields**: a JSON object mapping Plaky **item field keys** (from your board schema / API, e.g. status or custom column keys) to values Plaky accepts (exact option label, option id, assignee id, etc.). Omit **fields** entirely if you are unsure — items will still be created in the target group.

Rules:
- Tasks must be actionable and specific; scale count to initiative size (small fix = few tasks, large goal = more).
- Do not duplicate items already listed as open issues or existing Plaky lines.
- Align with DIRECTION.md when present.
- Every task must have evidence-based reasoning in fields above (no generic backlog filler).
- If data is missing, use unknowns/assumptions instead of guessing.
"""


async def run_repo_scan(
    session: AsyncSession,
    repo_full: str,
    *,
    dry_run: bool,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    if not settings.github_pat:
        return {"ok": False, "message": "GITHUB_PAT not configured"}

    parts = repo_full.split("/")
    if len(parts) != 2:
        return {"ok": False, "message": "repo must be owner/name"}
    owner, repo = parts[0], parts[1]
    short = repo
    routing, routing_source = await get_routing_async(
        repo_full, short, settings.github_org, with_source=True
    )

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
        parsed = parse_json_tasks(raw)
        tasks, parse_warnings = _normalize_scan_tasks(parsed)
        if not tasks:
            raise ValueError("Model returned no valid tasks after normalization")

        scan_row.tasks_proposed = json.dumps(tasks)[:65000]

        created = 0
        plaky = PlakyClient()
        cat = routing.plaky_table if routing else ""
        routing_note = (
            f"\n\n**Plaky group (label):** `{cat}`\n**Repo:** {repo_full}\n"
            if cat
            else f"\n\n**Repo:** {repo_full}\n"
        )
        bid, gid = effective_plaky_placement(routing if routing_source == "explicit" else None)
        qa_key_override: str | None = None
        if bid:
            from boardman.plaky.board_aware import board_person_field_keys, resolve_group_for_repo

            gid = await resolve_group_for_repo(bid, short, fallback_group_id=gid, plaky=plaky)
            keys = await board_person_field_keys(bid)
            if keys is not None:
                qa_key_override = keys.get("qa") or ""
        routing_warnings: list[str] = []
        if routing_source == "org_default":
            routing_warnings.append(
                "Repo has no explicit repos.yml routing; org default exists but placement was not auto-applied. "
                "Register repo-specific plaky_board_id/plaky_group_id to avoid ambiguous placement."
            )
        elif routing_source == "none":
            routing_warnings.append(
                "No routing found for repo; create used fallback behavior without explicit board/group placement."
            )
        default_assign = await build_assignment_field_map(
            repo_full, plaky_field_qa_key=qa_key_override
        )

        for item in tasks:
            title = str(item.get("title", "Task")).strip()
            desc = str(item.get("description", "")).strip()
            pri = str(item.get("priority", "medium")).lower()
            full_title = f"[{short}] {title}"
            evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
            assumptions = (
                item.get("assumptions") if isinstance(item.get("assumptions"), list) else []
            )
            unknowns = item.get("unknowns") if isinstance(item.get("unknowns"), list) else []
            evidence_block = ""
            if evidence:
                evidence_block += "\n\n**Evidence**\n" + "\n".join(
                    f"- {str(x)[:240]}" for x in evidence[:8]
                )
            if assumptions:
                evidence_block += "\n\n**Assumptions**\n" + "\n".join(
                    f"- {str(x)[:240]}" for x in assumptions[:6]
                )
            if unknowns:
                evidence_block += "\n\n**Unknowns**\n" + "\n".join(
                    f"- {str(x)[:240]}" for x in unknowns[:6]
                )
            body = (desc + evidence_block + routing_note).strip()
            field_map: dict[str, Any] = dict(default_assign)
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
        goals = json.dumps(
            {"last_scan_id": scan_row.id, "tasks_parsed": len(tasks), "tasks_created": created}
        )
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
            "warnings": parse_warnings + routing_warnings,
        }
    except Exception as e:
        scan_row.error = str(e)[:2000]
        await session.flush()
        return {"ok": False, "message": str(e), "scan_id": scan_row.id}
