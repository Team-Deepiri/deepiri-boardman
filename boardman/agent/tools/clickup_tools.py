"""Agent tools for ClickUp. The ClickUp counterpart of ``plaky_tools``.

ClickUp has no board schema, groups or field-key patching, so there is no equivalent of
``plaky_board_schema``, ``plaky_match_group``, ``plaky_patch_item_fields``,
``plaky_review_board`` or ``plaky_save_task_preferences``. A *list* is the placement unit
(the agent's "board id" is a list id here), and people are plain names resolved against the
workspace members.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from langchain_core.tools import StructuredTool

from boardman.clickup.client import ClickUpClient, clickup_priority_label
from boardman.plaky.name_match import rank_plaky_rows
from boardman.settings import settings


def _client() -> ClickUpClient:
    return ClickUpClient()


def _dump(obj: Any, limit: int = 12000) -> str:
    return json.dumps(obj, default=str)[:limit]


def _resolve_list_id(list_id: str = "") -> str:
    from boardman.agent.tool_context import get_context_placement_id

    return (
        (list_id or "").strip()
        or (get_context_placement_id() or "").strip()
        or (settings.clickup_default_list_id or "").strip()
    )


def _norm_title(title: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).split())


def _slim_task(t: dict[str, Any]) -> dict[str, Any]:
    prio = t.get("priority") if isinstance(t.get("priority"), dict) else {}
    out = {
        "id": t.get("id"),
        "name": t.get("name"),
        "status": (t.get("status") or {}).get("status")
        if isinstance(t.get("status"), dict)
        else t.get("status"),
        "priority": prio.get("priority") or clickup_priority_label(prio.get("id")),
        "assignees": [
            a.get("username") or a.get("email") or str(a.get("id"))
            for a in (t.get("assignees") or [])
            if isinstance(a, dict)
        ],
        "list": (t.get("list") or {}).get("name") if isinstance(t.get("list"), dict) else None,
        "parent": t.get("parent"),
        "url": t.get("url"),
    }
    return {k: v for k, v in out.items() if v not in (None, "", [])}


async def _workspace_users() -> list[dict[str, Any]]:
    r = await _client().list_workspace_users()
    return r.get("users") or [] if r.get("ok") else []


def _match_person(query: str, users: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """Resolve a plain name or email to one workspace member. Returns (user, problem)."""
    q = (query or "").strip()
    if not q:
        return None, ""
    rows = [
        {"id": u["id"], "name": f"{u.get('name') or ''} {u.get('email') or ''}".strip()}
        for u in users
    ]
    ranked, best = rank_plaky_rows(rows, q)
    strong = [r for r in ranked if r["score"] >= 400]
    if len(strong) > 1 and strong[0]["score"] == strong[1]["score"]:
        names = ", ".join(r["name"] for r in strong[:4])
        return None, f"'{q}' is ambiguous: {names}"
    if not best:
        return None, f"no workspace member matches '{q}'"
    user = next((u for u in users if u["id"] == best["id"]), None)
    return user, ""


def _assignee_ids(user: dict[str, Any] | None) -> list[int] | None:
    return [int(user["id"])] if user and str(user["id"]).isdigit() else None


# -- read tools -----------------------------------------------------------------------------


async def _clickup_list_lists() -> str:
    """Every list (the ClickUp equivalent of a board) with id and name."""
    return _dump(await _client().list_boards())


async def _clickup_list_tasks(status: str = "all", list_id: str = "") -> str:
    lid = _resolve_list_id(list_id)
    r = await _client().get_tasks(status=status or "all", board_id=lid or None)
    tasks = r.get("tasks")
    if not isinstance(tasks, list):
        return _dump(r)
    by_status: dict[str, int] = {}
    owned = 0
    for t in tasks:
        key = str(t.get("status_name") or "unknown")
        by_status[key] = by_status.get(key, 0) + 1
        owned += 1 if t.get("assignees") else 0
    shown = [_slim_task(t) for t in tasks[: settings.clickup_list_limit]]
    body: dict[str, Any] = {
        "ok": True,
        "list_id": lid,
        "applied_status_filter": status,
        "returned": len(shown),
        "total": len(tasks),
        "truncated": len(tasks) > len(shown),
        "count_by_status": by_status,
        "with_owner_count": owned,
        "tasks": shown,
    }
    if body["truncated"]:
        body["note"] = (
            f"Showing {len(shown)} of {len(tasks)} tasks. Do NOT state that something is absent "
            "from the list based on this partial view."
        )
    return json.dumps(body, default=str)


async def _clickup_get_task(task_id: str) -> str:
    r = await _client().get_task(task_id)
    if r.get("ok"):
        r = {
            **r,
            "task": {
                **_slim_task(r["task"]),
                "description": (r["task"].get("description") or "")[:2000],
            },
        }
    return _dump(r)


async def _clickup_list_workspace_users(name_query: str = "") -> str:
    users = await _workspace_users()
    q = (name_query or "").strip()
    if not q:
        return _dump({"ok": True, "users": users[:200]})
    rows = [
        {"id": u["id"], "name": f"{u.get('name') or ''} {u.get('email') or ''}".strip()}
        for u in users
    ]
    ranked, best = rank_plaky_rows(rows, q)
    return _dump({"ok": True, "best": best, "matches": [r for r in ranked if r["score"] > 0][:10]})


# -- write tools ----------------------------------------------------------------------------


async def _create_one(
    c: ClickUpClient,
    row: dict[str, Any],
    *,
    list_id: str,
    users: list[dict[str, Any]],
    existing: dict[str, str],
) -> dict[str, Any]:
    title = str(row.get("title") or "").strip()
    if not title:
        return {"ok": False, "title": title, "message": "title is required"}
    dup = existing.get(_norm_title(title))
    if dup:
        return {
            "ok": True,
            "title": title,
            "duplicate": True,
            "task_id": dup,
            "message": "Already in ClickUp",
        }
    description = str(row.get("description") or "")
    repo = str(row.get("repo_tag") or "").strip()
    if repo:
        description = f"{description}\n\nRepo: {repo}".strip()
    person, problem = _match_person(str(row.get("assignee") or ""), users)
    r = await c.create_task(
        title,
        description,
        row.get("priority") or "medium",
        board_id=list_id,
        status=(str(row.get("status") or "").strip() or None),
        assignee_ids=_assignee_ids(person),
    )
    out = {
        "ok": bool(r.get("ok")),
        "title": title,
        "task_id": r.get("task_id"),
        "task_url": r.get("task_url"),
        "message": r.get("message"),
    }
    if problem:
        out["people_resolved"] = {"assignee": problem}
    return {k: v for k, v in out.items() if v is not None}


async def _clickup_create_tasks(tasks_json: str, list_id: str = "") -> str:
    """Create several tasks in one call. Rows already in the list (same title) are skipped."""
    try:
        rows = json.loads(tasks_json or "[]")
    except json.JSONDecodeError as e:
        return _dump({"ok": False, "status": 400, "message": f"tasks_json is not valid JSON: {e}"})
    if not isinstance(rows, list) or not rows:
        return _dump(
            {"ok": False, "status": 400, "message": "tasks_json must be a non-empty array"}
        )
    lid = _resolve_list_id(list_id)
    if not lid:
        return _dump(
            {
                "ok": False,
                "status": 400,
                "message": "No ClickUp list id. Pass list_id or set CLICKUP_DEFAULT_LIST_ID.",
            }
        )

    c = _client()
    existing_resp = await c.get_tasks(status="all", board_id=lid)
    existing = {
        _norm_title(str(t.get("name") or "")): str(t.get("id"))
        for t in (existing_resp.get("tasks") or [])
        if isinstance(t, dict)
    }
    users = (
        await _workspace_users()
        if any(isinstance(r, dict) and r.get("assignee") for r in rows)
        else []
    )
    sem = asyncio.Semaphore(max(1, settings.clickup_create_concurrency))

    async def run(row: Any) -> dict[str, Any]:
        if not isinstance(row, dict):
            return {"ok": False, "message": "each row must be an object"}
        async with sem:
            return await _create_one(c, row, list_id=lid, users=users, existing=existing)

    # Rows repeated inside one call must not both create.
    seen: set[str] = set()
    unique: list[Any] = []
    for row in rows:
        key = _norm_title(str(row.get("title") or "")) if isinstance(row, dict) else ""
        if key and key in seen:
            continue
        seen.add(key)
        unique.append(row)
    receipts = await asyncio.gather(*(run(r) for r in unique))
    return _dump(
        {
            "ok": all(r.get("ok") for r in receipts),
            "list_id": lid,
            "created": sum(1 for r in receipts if r.get("ok") and not r.get("duplicate")),
            "already_present": sum(1 for r in receipts if r.get("duplicate")),
            "receipts": receipts,
        }
    )


async def _clickup_create_task(
    title: str,
    description: str = "",
    priority: str = "medium",
    list_id: str = "",
    status: str = "",
    assignee: str = "",
    repo_tag: str = "",
) -> str:
    return await _clickup_create_tasks(
        json.dumps(
            [
                {
                    "title": title,
                    "description": description,
                    "priority": priority,
                    "status": status,
                    "assignee": assignee,
                    "repo_tag": repo_tag,
                }
            ]
        ),
        list_id,
    )


async def _resolve_people(assignee: str, qa: str) -> tuple[list[int] | None, str, dict[str, str]]:
    """Resolve plain names to (assignee ids to add, QA user id, problems by role)."""
    wanted = {"assignee": (assignee or "").strip(), "qa": (qa or "").strip()}
    if not any(wanted.values()):
        return None, "", {}
    users = await _workspace_users()
    problems: dict[str, str] = {}
    resolved: dict[str, dict[str, Any] | None] = {}
    for role, name in wanted.items():
        if not name:
            continue
        person, problem = _match_person(name, users)
        resolved[role] = person
        if problem:
            problems[role] = problem
    qa_person = resolved.get("qa")
    return (
        _assignee_ids(resolved.get("assignee")),
        (str(qa_person["id"]) if qa_person else ""),
        problems,
    )


async def _clickup_update_task(
    task_id: str,
    status: str = "",
    priority: str = "",
    title: str = "",
    description: str = "",
    assignee: str = "",
    qa: str = "",
    auto_assign_qa: bool = False,
    github_repo: str = "",
) -> str:
    from boardman.services.clickup_mutations import update_clickup_task
    from boardman.services.task_mutations import UpdateTaskInput

    add_ids, qa_id, problems = await _resolve_people(assignee, qa)
    nothing_to_write = not any(
        [status, priority, title, description, add_ids, qa_id, auto_assign_qa]
    )
    if problems and nothing_to_write:
        return _dump({"ok": False, "status": 400, "message": "; ".join(problems.values())})
    r = await update_clickup_task(
        task_id,
        UpdateTaskInput(
            status=status or None,
            priority=priority or None,
            title=title or None,
            description=description or None,
            qa_plaky_id=qa_id or None,
            auto_assign_qa=bool(auto_assign_qa) and not qa_id,
            github_repo=github_repo or None,
        ),
        add_assignee_ids=add_ids,
        client=_client(),
    )
    if problems:
        r["people_resolved"] = problems
    return _dump(r)


async def _clickup_add_comment(task_id: str, body: str) -> str:
    return _dump(await _client().add_comment(task_id, body))


async def _clickup_link_prs(task_id: str, pr_urls: str) -> str:
    from boardman.services.pr_link_comment import collect_pr_urls, format_pr_link_comment

    parts = [p for p in re.split(r"[\s,]+", (pr_urls or "").strip()) if re.match(r"https?://", p)]
    urls = collect_pr_urls(pr_url=None, pr_urls=parts or None)
    if not urls:
        return _dump({"ok": False, "status": 400, "message": "supply at least one PR URL"})
    comment = format_pr_link_comment(urls)
    r = dict(await _client().add_comment(task_id, comment))
    r["posted_comment_text"] = comment
    r["linked_pr_urls"] = urls
    return _dump(r)


async def _clickup_create_subtask(
    parent_task_id: str,
    title: str,
    description: str = "",
    priority: str = "",
    status: str = "",
    assignee: str = "",
) -> str:
    c = _client()
    person, problem = (
        _match_person(assignee, await _workspace_users())
        if (assignee or "").strip()
        else (None, "")
    )
    r = await c.create_subtask(
        parent_task_id, title, description, status=status or None, priority=priority or None
    )
    if r.get("ok") and person and r.get("task_id"):
        r["assignee_update"] = await c.update_task_fields(
            r["task_id"], add_assignee_ids=_assignee_ids(person)
        )
        r["assignee_update"].pop("task", None)
    r.pop("task", None)
    if problem:
        r["people_resolved"] = {"assignee": problem}
    return _dump(r)


def build_clickup_tools(*, allow_writes: bool) -> list[StructuredTool]:
    def tool(fn, name: str, description: str) -> StructuredTool:
        return StructuredTool.from_function(coroutine=fn, name=name, description=description)

    tools = [
        tool(
            _clickup_list_lists,
            "clickup_list_lists",
            "List every ClickUp list (the board equivalent) with id and name. Use when the current "
            "placement has no list id or the user asks what lists exist.",
        ),
        tool(
            _clickup_list_tasks,
            "clickup_list_tasks",
            "List tasks in a ClickUp list. Args: status ('all' default, 'open' = not closed, or a "
            "status name), optional list_id (else the current placement or CLICKUP_DEFAULT_LIST_ID).",
        ),
        tool(
            _clickup_get_task,
            "clickup_get_task",
            "Get one ClickUp task by id, with status, priority, assignees and description. Args: task_id.",
        ),
        tool(
            _clickup_list_workspace_users,
            "clickup_list_workspace_users",
            "List ClickUp workspace members, or rank them by name_query. You rarely need this: "
            "assignee arguments on the create/update tools take a plain name.",
        ),
    ]
    if allow_writes:
        tools.extend(
            [
                tool(
                    _clickup_create_tasks,
                    "clickup_create_tasks",
                    "Create SEVERAL ClickUp tasks in ONE call, the only correct way to create 2+ tasks. "
                    "Args: tasks_json = JSON array of {title (required), description?, priority? "
                    "(urgent|high|medium|low), status?, repo_tag?, assignee? (PLAIN NAME or email, resolved "
                    "server-side, never pass an id)}; list_id? applies to all. Rows whose title is "
                    "already in the list are skipped and reported as 'Already in ClickUp'; keep those "
                    "lines in your reply and never claim they are new. An unresolvable assignee is "
                    "reported in people_resolved and left empty; relay that and never invent a person.",
                ),
                tool(
                    _clickup_create_task,
                    "clickup_create_task",
                    "Create ONE ClickUp task. Args: title, description, priority (urgent|high|medium|low), "
                    "list_id?, status?, assignee? (plain name), repo_tag?. Use clickup_create_tasks for 2 or more.",
                ),
                tool(
                    _clickup_update_task,
                    "clickup_update_task",
                    "Update a ClickUp task: status, priority, title, description, assignee (plain name, "
                    "added), qa (plain name), or auto_assign_qa=true with github_repo (owner/repo) to "
                    "pick QA from team_assignments.yml. QA is only set when asked.",
                ),
                tool(
                    _clickup_add_comment,
                    "clickup_add_comment",
                    "Add a comment to a ClickUp task. Args: task_id, body.",
                ),
                tool(
                    _clickup_link_prs,
                    "clickup_link_prs",
                    "Link GitHub PR URLs to a ClickUp task with a consistently formatted comment. "
                    "Args: task_id, pr_urls (one or more URLs).",
                ),
                tool(
                    _clickup_create_subtask,
                    "clickup_create_subtask",
                    "Create a subtask under parent_task_id (it lands in the parent's list). Args: "
                    "parent_task_id, title, description?, priority?, status?, assignee? (plain name).",
                ),
            ]
        )
    return tools
