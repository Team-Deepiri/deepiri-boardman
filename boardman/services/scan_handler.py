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
from boardman.plaky.placement_resolve import resolve_scan_placement
from boardman.repos_config import get_routing
from boardman.settings import settings

from boardman.services.local_scan_context import gather_local_scan_context


def _resolve_scan_provider_model(
    provider: Optional[str],
    model: Optional[str],
) -> tuple[str, str]:
    prov = (provider or settings.llm_provider or "ollama").lower()
    if prov in ("claude",):
        prov = "anthropic"
    if prov == "ollama":
        mdl = effective_ollama_model(model)
    else:
        mdl = (model or settings.llm_model or "").strip()
        if prov in ("anthropic", "claude"):
            prov = "anthropic"
            mdl = mdl or "claude-sonnet-4-20250514"
        elif prov in ("openai", "gpt"):
            mdl = mdl or "gpt-4o-mini"
        elif prov in ("gemini", "google"):
            mdl = mdl or "gemini-2.0-flash"
        elif prov in ("openrouter", "or"):
            mdl = mdl or "openai/gpt-4o-mini"
    return prov, mdl


async def _plaky_titles_on_board(board_id: str) -> str:
    plaky = PlakyClient()
    if not board_id.strip() or not plaky._public_root():
        return "(no board scope for existing-task hints)"
    r = await plaky.list_board_items(board_id.strip(), max_pages=1)
    if not r.get("ok"):
        return f"(could not list board items: {r.get('message')})"
    items = r.get("items") or []
    lines: List[str] = []
    for it in items[:45]:
        if isinstance(it, dict):
            t = str(it.get("title") or "").strip()
            if t:
                lines.append(f"- {t[:120]}")
    return "\n".join(lines) if lines else "(no items on first page)"


def _local_scan_prompt(
    project_label: str,
    category: str,
    bundle: Dict[str, Any],
    plaky_lines: str,
) -> str:
    top = bundle.get("top_level") or []
    top_s = ", ".join(str(x) for x in top[:40]) if top else "(none)"
    doc_blocks: List[str] = []
    for d in bundle.get("doc_excerpts") or []:
        if isinstance(d, dict):
            p = str(d.get("path") or "")
            ex = str(d.get("excerpt") or "")
            if p and ex:
                doc_blocks.append(f"### {p}\n{ex}")
    docs_joined = "\n\n".join(doc_blocks) if doc_blocks else "(no extra doc excerpts)"
    direction = str(bundle.get("direction_md") or "(missing DIRECTION.md)")
    readme = str(bundle.get("readme_excerpt") or "(missing README)")
    root = str(bundle.get("root") or project_label)

    return f"""You are a software project manager. Propose concrete Plaky tasks from a **local codebase** (no GitHub API data for this request).

PROJECT: {project_label}
LOCAL ROOT: {root}
CATEGORY (from repos.yml if a github_repo was supplied): {category or "unknown"}

TOP-LEVEL ENTRIES:
{top_s}

DIRECTION.md:
{direction}

README (excerpt):
{readme}

OTHER DOCS (excerpts):
{docs_joined}

EXISTING PLAKY TASKS ON TARGET BOARD (first page — avoid duplicates):
{plaky_lines}

Return ONLY a JSON array of 3-18 objects, no markdown fences:
[
  {{"title": "short title", "description": "markdown body", "priority": "low|medium|high", "fields": {{}}}},
  ...
]

Each object may include optional **fields**: Plaky item field keys → values. Omit **fields** if unsure.

Rules:
- Tasks must be actionable; ground them in DIRECTION.md / README / doc excerpts above.
- Do not duplicate titles clearly listed under existing Plaky tasks.
- If direction is thin, infer from README and doc excerpts but state assumptions in descriptions where needed.
"""


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
    plaky_board_query: Optional[str] = None,
    plaky_group_query: Optional[str] = None,
) -> Dict[str, Any]:
    if not settings.github_pat:
        return {"ok": False, "message": "GITHUB_PAT not configured"}

    parts = repo_full.split("/")
    if len(parts) != 2:
        return {"ok": False, "message": "repo must be owner/name"}
    owner, repo = parts[0], parts[1]
    short = repo
    routing = get_routing(repo_full, short, settings.github_org)

    bid_o, gid_o = effective_plaky_placement(routing)
    gfallback = (routing.plaky_table if routing else "") or ""
    bid, gid, place_meta = await resolve_scan_placement(
        board_id=bid_o,
        group_id=gid_o,
        group_fallback_name=gfallback,
        board_name_query=(plaky_board_query or "").strip(),
        group_name_query=(plaky_group_query or "").strip(),
    )
    if not bid or not gid:
        extras = [m for m in (place_meta.get("board_error"), place_meta.get("group_error")) if m]
        tail = (
            "Set repos.yml plaky_board_id or pass plaky_board_query (fuzzy board name)."
            if not bid
            else "Set repos.yml plaky_group_id, plaky_table (group name hint), or plaky_group_query."
        )
        msg = "Plaky placement incomplete for scan."
        if extras:
            msg += " " + " ".join(str(x) for x in extras)
        msg += " " + tail
        return {"ok": False, "message": msg.strip(), "placement": place_meta}

    prov, mdl = _resolve_scan_provider_model(provider, model)

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
        default_assign = await build_assignment_field_map(repo_full)

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
        out: Dict[str, Any] = {
            "ok": True,
            "dry_run": dry_run,
            "tasks_parsed": len(tasks),
            "tasks_created": created if not dry_run else 0,
            "preview": cap,
            "scan_id": scan_row.id,
        }
        if place_meta:
            out["placement"] = place_meta
        return out
    except Exception as e:
        scan_row.error = str(e)[:2000]
        await session.flush()
        return {"ok": False, "message": str(e), "scan_id": scan_row.id}


async def run_local_path_scan(
    session: AsyncSession,
    path: str,
    *,
    dry_run: bool,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    plaky_board_id: Optional[str] = None,
    plaky_group_id: Optional[str] = None,
    plaky_board_query: Optional[str] = None,
    plaky_group_query: Optional[str] = None,
    github_repo: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Suggest (and optionally create) Plaky tasks from a **local directory** only.

    Does **not** call the GitHub API. **github_repo** is optional: use it for repos.yml
    placement/assignment when ``plaky_board_id`` / ``plaky_group_id`` are omitted, and for
    team field maps. Otherwise pass explicit board and group ids.

    **plaky_board_query** / **plaky_group_query** fuzzy-match names via the Plaky API when
    ids are missing (same ranking as **plaky_match_board** / **plaky_match_group**).
    """
    bundle = gather_local_scan_context(path)
    if not bundle.get("ok"):
        return {"ok": False, "message": bundle.get("message", "invalid path")}

    root_str = str(bundle.get("root") or "")
    scan_key = f"local:{root_str}"[:255]

    gh = (github_repo or "").strip()
    routing = None
    if gh:
        parts = gh.split("/")
        if len(parts) != 2:
            return {"ok": False, "message": "github_repo must be owner/name when provided"}
        routing = get_routing(gh, parts[1], settings.github_org)

    bid_o, gid_o = effective_plaky_placement(routing) if routing else ("", "")
    bid_merged = (plaky_board_id or "").strip() or bid_o
    gid_merged = (plaky_group_id or "").strip() or gid_o
    gfallback = (routing.plaky_table if routing else "") or ""
    bid, gid, place_meta = await resolve_scan_placement(
        board_id=bid_merged,
        group_id=gid_merged,
        group_fallback_name=gfallback,
        board_name_query=(plaky_board_query or "").strip(),
        group_name_query=(plaky_group_query or "").strip(),
    )

    if not bid or not gid:
        extras = [m for m in (place_meta.get("board_error"), place_meta.get("group_error")) if m]
        msg = (
            "Plaky placement incomplete. Pass plaky_board_id or plaky_board_query (fuzzy), "
            "and plaky_group_id, plaky_group_query, or github_repo with repos.yml "
            "(plaky_group_id / plaky_table for group name hint)."
        )
        if extras:
            msg = " ".join([msg] + [str(x) for x in extras])
        return {"ok": False, "message": msg.strip(), "placement": place_meta}

    prov, mdl = _resolve_scan_provider_model(provider, model)

    scan_row = ScanRun(
        github_repo=scan_key,
        provider=prov,
        model=mdl,
        dry_run=dry_run,
        tasks_created=0,
    )
    session.add(scan_row)
    await session.flush()

    try:
        if gh:
            short = gh.split("/")[-1]
            plaky_lines = await fetch_plaky_titles_for_repo(gh, short)
        else:
            plaky_lines = await _plaky_titles_on_board(bid)

        project_label = gh or bundle.get("project_name") or root_str
        category = routing.category if routing else ""
        prompt = _local_scan_prompt(project_label, category, bundle, plaky_lines)

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
        short = gh.split("/")[-1] if gh else str(bundle.get("project_name") or "local")
        cat = routing.plaky_table if routing else ""
        routing_note = (
            f"\n\n**Source:** local scan\n**Path:** `{root_str}`\n"
            f"{f'**GitHub repo (context):** `{gh}`\n' if gh else ''}"
            f"{f'**Plaky group (label):** `{cat}`\n' if cat else ''}"
        )
        default_assign: Dict[str, Any] = {}
        if gh:
            default_assign = await build_assignment_field_map(gh)

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

        direction = str(bundle.get("direction_md") or "")
        q = select(ProjectContext).where(ProjectContext.repo == scan_key)
        pc = (await session.execute(q)).scalar_one_or_none()
        summary = direction[:12000]
        goals = json.dumps(
            {
                "last_scan_id": scan_row.id,
                "tasks_parsed": len(tasks),
                "tasks_created": created,
                "local_root": root_str,
            }
        )
        if pc is None:
            session.add(
                ProjectContext(
                    repo=scan_key,
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
        out: Dict[str, Any] = {
            "ok": True,
            "dry_run": dry_run,
            "tasks_parsed": len(tasks),
            "tasks_created": created if not dry_run else 0,
            "preview": cap,
            "scan_id": scan_row.id,
            "local_root": root_str,
            "scan_mode": "local_path",
        }
        if place_meta:
            out["placement"] = place_meta
        return out
    except Exception as e:
        scan_row.error = str(e)[:2000]
        await session.flush()
        return {"ok": False, "message": str(e), "scan_id": scan_row.id}
