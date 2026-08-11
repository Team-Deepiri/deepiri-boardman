"""LangChain tools wrapping PlakyClient (async)."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import StructuredTool

from boardman.assignment.config import infer_plaky_field_keys_from_normalized, load_team_assignments
from boardman.assignment.qa_picker import build_repo_field_map, normalize_github_repo_inputs
from boardman.plaky.board_schema import (
    fetch_board_schema_bundle,
    plaky_repo_field_value_format,
    resolve_repo_tag_field_values_from_schema,
    validate_field_values_detailed,
)
from boardman.plaky.client import PlakyClient
from boardman.plaky.name_match import rank_plaky_rows
from boardman.plaky.task_tag_vocab import (
    canonical_task_priority,
)
from boardman.services.task_mutations import (
    CreateSubtaskInput,
    CreateTaskInput,
    UpdateTaskInput,
    create_subtask_internal,
    create_task_internal,
    update_task_internal,
)


async def _plaky_list_boards() -> str:
    """Return all boards (id + name) from Plaky — use when placement is unset or user asks what exists."""
    c = PlakyClient()
    raw = await c.list_boards()
    return json.dumps(raw, default=str)[:12000]


async def _plaky_list_tasks(status: str = "open", board_id: str = "") -> str:
    from boardman.agent.tool_context import get_context_plaky_board_id

    c = PlakyClient()
    bid = (board_id or "").strip() or (get_context_plaky_board_id() or "").strip() or None
    r = await c.get_tasks(status=status, board_id=bid)
    return json.dumps(r, default=str)[:12000]


async def _plaky_get_task(task_id: str) -> str:
    c = PlakyClient()
    r = await c.get_task(task_id)
    return json.dumps(r, default=str)[:12000]


async def _plaky_get_board_item(board_id: str, item_id: str) -> str:
    """Full item on v1/public (field keys / values as Plaky returns them)."""
    c = PlakyClient()
    r = await c.get_board_item_public(board_id.strip(), item_id.strip())
    return json.dumps(r, default=str)[:12000]


async def _plaky_match_board(name_query: str) -> str:
    """List boards from Plaky API and rank by name vs `name_query` (e.g. user's board mention)."""
    c = PlakyClient()
    raw = await c.list_boards()
    boards = raw.get("boards") or []
    if not isinstance(boards, list):
        boards = []
    matches, best = rank_plaky_rows(boards, name_query)
    return json.dumps(
        {
            "list_ok": raw.get("ok"),
            "message": raw.get("message"),
            "matches": matches[:25],
            "best": best,
        },
        default=str,
    )[:12000]


async def _plaky_board_schema(board_id: str) -> str:
    """Return groups + field definitions (status/type/priority options) for a board from the Plaky API."""
    bundle = await fetch_board_schema_bundle(board_id)
    out = {
        "ok": bundle.get("ok"),
        "message": bundle.get("message"),
        "board_fetch_ok": bundle.get("board_fetch_ok"),
        "groups_fetch_ok": bundle.get("groups_fetch_ok"),
        "normalized": bundle.get("normalized"),
        "markdown": bundle.get("markdown"),
    }
    return json.dumps(out, default=str)[:12000]


async def _plaky_match_group(board_id: str, name_query: str) -> str:
    """List groups on `board_id` and rank by name vs `name_query`."""
    c = PlakyClient()
    raw = await c.list_groups(board_id)
    groups = raw.get("groups") or []
    if not isinstance(groups, list):
        groups = []
    matches, best = rank_plaky_rows(groups, name_query)
    return json.dumps(
        {
            "list_ok": raw.get("ok"),
            "message": raw.get("message"),
            "matches": matches[:25],
            "best": best,
        },
        default=str,
    )[:12000]


async def _plaky_list_workspace_users(name_query: str = "") -> str:
    """Plaky workspace users (assignee lookup). Optional name_query ranks by display name."""
    c = PlakyClient()
    r = await c.list_workspace_users()
    users = r.get("users") or []
    if not isinstance(users, list):
        users = []
    if not (name_query or "").strip():
        return json.dumps(
            {"ok": r.get("ok"), "message": r.get("message"), "users": users[:200]},
            default=str,
        )[:12000]
    matches, best = rank_plaky_rows(users, name_query)
    return json.dumps(
        {
            "ok": r.get("ok"),
            "message": r.get("message"),
            "matches": matches[:40],
            "best": best,
        },
        default=str,
    )[:12000]


def _field_text(item: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _normalize_title_key(title: str) -> str:
    return " ".join((title or "").strip().lower().split())


def _has_acceptance_content(desc: str) -> bool:
    d = (desc or "").lower()
    return "acceptance" in d or "done when" in d or "definition of done" in d


async def _plaky_review_board(board_id: str = "", group_id: str = "", max_items: int = 200) -> str:
    """Read-only board diagnosis used in REVIEW/preview mode before any write action.

    Returns JSON summarizing duplicate-title clusters, items missing acceptance
    criteria, and stale-looking items. Safe to call when ``allow_writes=False``.
    """
    from boardman.agent.tool_context import (
        get_context_plaky_board_id,
        get_context_plaky_group_id,
    )

    bid = (board_id or "").strip() or (get_context_plaky_board_id() or "")
    gid = (group_id or "").strip() or (get_context_plaky_group_id() or "")
    if not bid:
        return json.dumps(
            {"ok": False, "message": "board_id missing (pass arg or set current placement)"}
        )

    c = PlakyClient()
    lim = max(1, min(int(max_items or 200), 600))
    raw = await c.list_board_items(bid, max_pages=max(1, (lim // 100) + 1))
    if not raw.get("ok"):
        return json.dumps(
            {"ok": False, "message": raw.get("message") or "Could not load board items"}
        )
    items = raw.get("items") or []
    if not isinstance(items, list):
        items = []
    if gid:
        filtered: list[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            g = it.get("group") if isinstance(it.get("group"), dict) else {}
            candidate = str(it.get("groupId") or it.get("group_id") or g.get("id") or "").strip()
            if candidate == gid:
                filtered.append(it)
        items = filtered
    items = [it for it in items if isinstance(it, dict)][:lim]

    by_title: dict[str, list[dict[str, Any]]] = {}
    missing_acceptance: list[dict[str, Any]] = []
    stale_candidates: list[dict[str, Any]] = []
    done_like = 0
    for it in items:
        title = _field_text(it, "name", "title", "summary")
        desc = _field_text(it, "description", "body", "text")
        item_id = _field_text(it, "id", "itemId", "_id")
        status = _field_text(it, "status", "state")
        if "done" in status.lower() or "closed" in status.lower():
            done_like += 1
        tkey = _normalize_title_key(title)
        if tkey:
            by_title.setdefault(tkey, []).append({"id": item_id, "title": title, "status": status})
        if title and not _has_acceptance_content(desc):
            missing_acceptance.append({"id": item_id, "title": title, "status": status})
        updated = _field_text(
            it, "updatedAt", "updated_at", "lastUpdatedAt", "createdAt", "created_at"
        )
        if updated and ("2023" in updated or "2024" in updated):
            stale_candidates.append(
                {"id": item_id, "title": title, "updated": updated, "status": status}
            )

    duplicate_clusters = [
        {"title_key": k, "items": vals}
        for k, vals in by_title.items()
        if len(vals) > 1 and k not in {"", "task"}
    ]
    duplicate_clusters = sorted(duplicate_clusters, key=lambda x: len(x["items"]), reverse=True)[
        :20
    ]

    summary = {
        "ok": True,
        "board_id": bid,
        "group_id": gid or None,
        "items_scanned": len(items),
        "done_like_count": done_like,
        "duplicate_cluster_count": len(duplicate_clusters),
        "missing_acceptance_count": len(missing_acceptance),
        "stale_candidate_count": len(stale_candidates),
        "duplicate_clusters": duplicate_clusters,
        "missing_acceptance": missing_acceptance[:40],
        "stale_candidates": stale_candidates[:40],
        "recommended_actions": [
            "Merge/close duplicate clusters first.",
            "Add acceptance criteria to high-priority items missing clear done conditions.",
            "Review stale items for archive, rewrite, or split.",
        ],
    }
    return json.dumps(summary, default=str)[:15000]


async def _plaky_save_task_preferences(preferences_json: str) -> str:
    """
    Persist assignee + field defaults for this chat session (merged into the next plaky_create_task).
    JSON keys: field_values (object), optional engineer_plaky_id, qa_plaky_id, summary,
    replace_field_values (bool, default false clears then applies only provided field_values).
    """
    from boardman.agent.task_draft import save_task_draft_merge
    from boardman.agent.tool_context import get_agent_session_pk, get_tool_db_session

    db = get_tool_db_session()
    pk = get_agent_session_pk()
    if db is None or pk is None:
        return json.dumps(
            {
                "ok": False,
                "message": "No agent session bound to this request (internal).",
            }
        )
    try:
        p = json.loads((preferences_json or "").strip() or "{}")
    except json.JSONDecodeError:
        return json.dumps({"ok": False, "message": "preferences_json must be valid JSON"})
    if not isinstance(p, dict):
        return json.dumps({"ok": False, "message": "preferences_json must be a JSON object"})

    fv = p.get("field_values")
    if fv is not None and not isinstance(fv, dict):
        return json.dumps({"ok": False, "message": "field_values must be an object"})

    out = await save_task_draft_merge(
        db,
        pk,
        field_values_patch=fv if isinstance(fv, dict) else {},
        engineer_plaky_id=str(p.get("engineer_plaky_id") or ""),
        qa_plaky_id=str(p.get("qa_plaky_id") or ""),
        summary=str(p.get("summary") or ""),
        replace_field_values=bool(p.get("replace_field_values", False)),
    )
    return json.dumps(out, default=str)


async def _plaky_create_task(
    title: str,
    description: str,
    priority: str = "Medium",
    repo_tag: str = "",
    board_id: str = "",
    group_id: str = "",
    field_values_json: str = "",
    auto_assign_team: bool = True,
) -> str:
    from boardman.agent.task_draft import load_task_draft, merge_draft_into_field_values
    from boardman.agent.tool_context import (
        get_agent_session_pk,
        get_context_plaky_board_id,
        get_context_plaky_group_id,
        get_tool_db_session,
    )

    bid = board_id.strip() or get_context_plaky_board_id() or None
    gid = group_id.strip() or get_context_plaky_group_id() or None
    repo_tokens = normalize_github_repo_inputs(extra_repo_text=repo_tag)

    parsed: dict[str, Any] = {}
    raw_f = (field_values_json or "").strip()
    if raw_f:
        try:
            loaded = json.loads(raw_f)
        except json.JSONDecodeError:
            return json.dumps(
                {"ok": False, "message": "field_values_json must be valid JSON object"}
            )
        if not isinstance(loaded, dict):
            return json.dumps(
                {
                    "ok": False,
                    "message": "field_values_json must be a JSON object of fieldKey -> value",
                }
            )
        parsed = loaded

    db = get_tool_db_session()
    pk = get_agent_session_pk()
    if db is not None and pk is not None:
        draft = await load_task_draft(db, pk)
        merged = merge_draft_into_field_values(draft, parsed)
    else:
        merged = dict(parsed)

    effective_board = (bid or get_context_plaky_board_id() or "").strip() or None
    normalized: dict[str, Any] | None = None
    bundle: dict[str, Any] | None = None
    if effective_board and (repo_tokens or merged):
        bundle = await fetch_board_schema_bundle(effective_board)
        normalized = (
            bundle.get("normalized") if isinstance(bundle.get("normalized"), dict) else None
        )

    if repo_tokens:
        cfg = load_team_assignments()
        inf_tags = infer_plaky_field_keys_from_normalized(normalized) if normalized else {}
        repo_k = (cfg.plaky_field_repo or inf_tags.get("repo") or "").strip()
        gh_k = (cfg.plaky_field_github_repos or inf_tags.get("github_repos") or "").strip()
        repo_fmt = plaky_repo_field_value_format(normalized, repo_k)
        gh_fmt = plaky_repo_field_value_format(normalized, gh_k)
        if repo_k == gh_k and repo_k and (repo_fmt == "short" or gh_fmt == "short"):
            repo_fmt = gh_fmt = "short"
        repo_fields = build_repo_field_map(
            cfg,
            repo_value=repo_tokens[0],
            github_repos=repo_tokens,
            repo_value_format=repo_fmt,
            github_repos_value_format=gh_fmt,
        )
        for key, value in repo_fields.items():
            if key not in parsed:
                merged[key] = value

    field_validation_warnings: list[str] = []
    if merged:
        if normalized is None and effective_board:
            bundle = await fetch_board_schema_bundle(effective_board)
            normalized = (
                bundle.get("normalized") if isinstance(bundle.get("normalized"), dict) else None
            )
        if normalized:
            cfg_tags = load_team_assignments()
            inf_tags = infer_plaky_field_keys_from_normalized(normalized)
            tag_keys = {
                x
                for x in (
                    (cfg_tags.plaky_field_repo or "").strip(),
                    (cfg_tags.plaky_field_github_repos or "").strip(),
                    (inf_tags.get("repo") or "").strip(),
                    (inf_tags.get("github_repos") or "").strip(),
                )
                if x
            }
            if tag_keys:
                resolve_repo_tag_field_values_from_schema(merged, normalized, keys=tag_keys)
        cleaned, errors, warnings = validate_field_values_detailed(
            merged,
            normalized,
            options_check=True,
            board_id=effective_board or "",
            schema_fetch_ok=bundle.get("ok") if isinstance(bundle, dict) else None,
            schema_fetch_message=str((bundle or {}).get("message") or ""),
        )
        if errors:
            return json.dumps(
                {
                    "ok": False,
                    "message": "field_values_json contains invalid keys/values for board schema",
                    "errors": errors,
                    "warnings": warnings,
                },
                default=str,
            )
        merged = cleaned
        field_validation_warnings = warnings

    canon_pri = canonical_task_priority(priority)
    r = await create_task_internal(
        CreateTaskInput(
            title=title,
            description=description,
            priority=canon_pri,
            github_repos=repo_tokens if repo_tokens else None,
            plaky_board_id=bid,
            plaky_group_id=gid,
            field_values=merged if merged else None,
            auto_assign_team=auto_assign_team,
        )
    )
    out = dict(r) if isinstance(r, dict) else {"result": r}
    if merged:
        out["merged_field_values"] = merged
    if field_validation_warnings:
        out["field_validation_warnings"] = field_validation_warnings
    return json.dumps(out, default=str)


async def _plaky_patch_item_fields(board_id: str, item_id: str, fields_json: str) -> str:
    """PATCH custom/board fields on an existing item (v1/public). fields_json: {\"fieldKey\": value, ...}."""
    try:
        parsed = json.loads((fields_json or "").strip() or "{}")
    except json.JSONDecodeError:
        return json.dumps({"ok": False, "message": "fields_json must be valid JSON object"})
    if not isinstance(parsed, dict):
        return json.dumps({"ok": False, "message": "fields_json must be a JSON object"})
    bid = board_id.strip()
    field_validation_warnings: list[str] = []
    if parsed:
        bundle = await fetch_board_schema_bundle(bid)
        normalized = (
            bundle.get("normalized") if isinstance(bundle.get("normalized"), dict) else None
        )
        if normalized:
            cfg_tags = load_team_assignments()
            inf_tags = infer_plaky_field_keys_from_normalized(normalized)
            tag_keys = {
                x
                for x in (
                    (cfg_tags.plaky_field_repo or "").strip(),
                    (cfg_tags.plaky_field_github_repos or "").strip(),
                    (inf_tags.get("repo") or "").strip(),
                    (inf_tags.get("github_repos") or "").strip(),
                )
                if x
            }
            if tag_keys:
                resolve_repo_tag_field_values_from_schema(parsed, normalized, keys=tag_keys)
        cleaned, errors, warnings = validate_field_values_detailed(
            parsed,
            normalized,
            options_check=True,
            board_id=bid,
            schema_fetch_ok=bundle.get("ok") if isinstance(bundle, dict) else None,
            schema_fetch_message=str((bundle or {}).get("message") or ""),
        )
        if errors:
            return json.dumps(
                {
                    "ok": False,
                    "message": "fields_json contains invalid keys/values for board schema",
                    "errors": errors,
                    "warnings": warnings,
                },
                default=str,
            )
        parsed = cleaned
        field_validation_warnings = warnings
    c = PlakyClient()
    r = await c.patch_item_field_values(bid, item_id.strip(), parsed)
    out = dict(r) if isinstance(r, dict) else {"result": r}
    if field_validation_warnings:
        out["field_validation_warnings"] = field_validation_warnings
    return json.dumps(out, default=str)


async def _plaky_update_task(
    task_id: str,
    status: str | None = None,
    task_type: str | None = None,
    priority: str | None = None,
    qa_plaky_id: str | None = None,
    auto_assign_qa: bool = False,
    github_repo: str | None = None,
    board_id: str = "",
) -> str:
    from boardman.agent.tool_context import get_context_plaky_board_id

    bid = (board_id or "").strip() or (get_context_plaky_board_id() or "").strip() or None
    gh = (github_repo or "").strip() or None
    r = await update_task_internal(
        task_id,
        UpdateTaskInput(
            status=status,
            task_type=task_type,
            priority=priority,
            qa_plaky_id=qa_plaky_id,
            auto_assign_qa=auto_assign_qa,
            github_repo=gh,
            plaky_board_id=bid,
        ),
    )
    return json.dumps(r, default=str)


async def _plaky_add_comment(task_id: str, body: str, board_id: str = "") -> str:
    from boardman.agent.tool_context import get_context_plaky_board_id

    c = PlakyClient()
    bid = (board_id or "").strip() or (get_context_plaky_board_id() or "").strip() or None
    r = await c.add_comment(task_id, body, board_id=bid)
    return json.dumps(r, default=str)


async def _plaky_link_prs(task_id: str, pr_urls: str, board_id: str = "") -> str:
    """
    Link one or more GitHub PR URLs to a Plaky item by posting a consistently-formatted comment.

    `pr_urls` may be a single URL or a comma/whitespace/newline-separated list.
    """
    import re

    from boardman.agent.tool_context import get_context_plaky_board_id
    from boardman.services.pr_link_comment import collect_pr_urls, format_pr_link_comment

    raw = (pr_urls or "").strip()
    parts = [p for p in re.split(r"[\s,]+", raw) if p.strip()]
    urls = collect_pr_urls(pr_url=None, pr_urls=parts or None)
    if not urls:
        return json.dumps(
            {"ok": False, "status": 400, "message": "supply at least one PR URL"}, default=str
        )

    c = PlakyClient()
    bid = (board_id or "").strip() or (get_context_plaky_board_id() or "").strip() or None
    comment = format_pr_link_comment(urls)
    r = await c.add_comment(task_id, comment, board_id=bid)
    r2 = dict(r) if isinstance(r, dict) else {"ok": False, "message": "invalid result"}
    r2["posted_comment_text"] = comment
    r2["linked_pr_urls"] = urls
    return json.dumps(r2, default=str)


async def _plaky_create_subtask(
    parent_task_id: str,
    title: str,
    description: str = "",
    priority: str = "Medium",
    status: str = "In Progress",
    task_type: str = "Feature",
    repo_tag: str = "",
    engineer_plaky_id: str = "",
    qa_plaky_id: str = "",
    auto_assign_qa: bool = True,
    board_id: str = "",
    group_id: str = "",
) -> str:
    from boardman.agent.tool_context import get_context_plaky_board_id, get_context_plaky_group_id

    bid = (board_id or "").strip() or (get_context_plaky_board_id() or "").strip() or None
    gid = (group_id or "").strip() or (get_context_plaky_group_id() or "").strip() or None
    repo_tokens = normalize_github_repo_inputs(extra_repo_text=repo_tag)
    r = await create_subtask_internal(
        CreateSubtaskInput(
            parent_task_id=parent_task_id,
            title=title,
            description=description,
            priority=priority,
            status=status,
            task_type=task_type,
            github_repos=repo_tokens if repo_tokens else None,
            engineer_plaky_id=(engineer_plaky_id or "").strip() or None,
            qa_plaky_id=(qa_plaky_id or "").strip() or None,
            auto_assign_qa=auto_assign_qa,
            plaky_board_id=bid,
            plaky_group_id=gid,
        )
    )
    return json.dumps(r, default=str)


def build_plaky_tools(*, allow_writes: bool) -> list[StructuredTool]:
    tools: list[StructuredTool] = [
        StructuredTool.from_function(
            coroutine=_plaky_list_boards,
            name="plaky_list_boards",
            description=(
                "List every Plaky board with id and name from the API. "
                "Use when **Current Plaky placement** is missing a board_id or the user asks what boards exist. "
                "If placement already lists board_id, do not call this unless switching boards."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_plaky_match_board,
            name="plaky_match_board",
            description=(
                "Find a Plaky board by fuzzy name match. "
                "Skip if **Current Plaky placement** already includes board_id — use that id instead. "
                "Args: name_query (e.g. user said 'Deepiri Main board'). "
                "Returns `best` with id when confident; otherwise pick from `matches` by score."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_plaky_match_group,
            name="plaky_match_group",
            description=(
                "Find a group (section) on a board by fuzzy name. Args: board_id, name_query "
                "(e.g. 'Backlog'). If **Current Plaky placement** lists group_id, use it — do not re-ask. "
                "Otherwise use board_id from placement or from plaky_match_board / plaky_list_boards."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_plaky_board_schema,
            name="plaky_board_schema",
            description=(
                "MUST call before plaky_create_task or plaky_patch_item_fields when you need field keys or allowed values. "
                "Returns groups + fields with key= and options. Args: board_id."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_plaky_list_tasks,
            name="plaky_list_tasks",
            description=(
                "List Plaky tasks. Args: status (open|done|... default open). "
                "Optional board_id (or Current Plaky placement) enables accurate listing/filtering on v1/public."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_plaky_get_task,
            name="plaky_get_task",
            description="Get one Plaky task by id (legacy /tasks). Args: task_id.",
        ),
        StructuredTool.from_function(
            coroutine=_plaky_get_board_item,
            name="plaky_get_board_item",
            description=(
                "Get one board item via Plaky v1/public (richer field payload than plaky_get_task). "
                "Args: board_id, item_id. Use to inspect field keys/values on an existing item."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_plaky_list_workspace_users,
            name="plaky_list_workspace_users",
            description=(
                "List Plaky workspace users (id + name) for assignee fields, or rank by name_query. "
                "Use after plaky_board_schema to map person fields — pass the user's name as name_query; "
                "use `best.id` or high-score match ids in field_values / plaky_save_task_preferences."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_plaky_save_task_preferences,
            name="plaky_save_task_preferences",
            description=(
                "Save assignee + Plaky field defaults for **this chat session** (persists in DB). "
                "Args: preferences_json — JSON with field_values {fieldKey: value}, optional "
                "engineer_plaky_id, qa_plaky_id (explicit Plaky user ids), summary, "
                "replace_field_values (bool). Next **plaky_create_task** merges these automatically."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_plaky_review_board,
            name="plaky_review_board",
            description=(
                "Read-only diagnosis of a Plaky board: duplicate-title clusters, items missing "
                "acceptance criteria, and stale-looking items. Use in **REVIEW/preview mode** before "
                "any bulk write to summarize what the user just asked you to organize. "
                "Args: board_id (defaults to current placement), group_id (optional, restrict to one section), "
                "max_items (1-600, default 200)."
            ),
        ),
    ]
    if allow_writes:
        tools.extend(
            [
                StructuredTool.from_function(
                    coroutine=_plaky_create_task,
                    name="plaky_create_task",
                    description=(
                        "Create a Plaky item. Call plaky_board_schema first if field_values_json is non-empty. "
                        "field_values_json keys MUST match schema key= strings; assignee ids from plaky_list_workspace_users. "
                        "Placement: pass board_id/group_id or rely on Current Plaky placement. "
                        "Args: title, description, priority (High|Low|Medium|Very Important or legacy low|medium|high), "
                        "repo_tag?, board_id?, group_id?, field_values_json?, auto_assign_team (default true). "
                        "Set auto_assign_team false to skip roster QA assignment; set QA explicitly via field_values_json / "
                        "session draft keys from plaky_board_schema instead. "
                        "When auto_assign_team is true and repo_tag lists a GitHub repo, team_assignments.yml picks QA "
                        "unless the QA person field is already set in field_values_json or saved draft. "
                        "Bare repo names (e.g. my-repo) normalize to GITHUB_BARE_REPO_OWNER/my-repo like the CLI. "
                        "repo_tag may include one or more owner/repo tokens separated by commas or new lines."
                    ),
                ),
                StructuredTool.from_function(
                    coroutine=_plaky_patch_item_fields,
                    name="plaky_patch_item_fields",
                    description=(
                        "PATCH item fields. Call plaky_board_schema(board_id) first; keys must match schema. "
                        "Args: board_id, item_id, fields_json object."
                    ),
                ),
                StructuredTool.from_function(
                    coroutine=_plaky_update_task,
                    name="plaky_update_task",
                    description=(
                        "Update workflow fields on an existing task: status, type, priority, QA assignment. "
                        "Use plaky_create_task for title/description/repo/engineer. "
                        "QA: pass qa_plaky_id for explicit assignee id, OR set auto_assign_qa true and github_repo (owner/repo "
                        "or bare repo name; roster uses team_assignments.yml like the CLI). Omit both to leave QA unchanged "
                        "(you can still update status/type/priority). Optional board_id or Current Plaky placement resolves "
                        "field keys for PATCH. Args: task_id; optional status, task_type, priority, qa_plaky_id, "
                        "auto_assign_qa (default false), github_repo, board_id."
                    ),
                ),
                StructuredTool.from_function(
                    coroutine=_plaky_add_comment,
                    name="plaky_add_comment",
                    description=(
                        "Add a comment to a Plaky task (v1/public uses board item comments). "
                        "Args: task_id, body (markdown). Optional board_id or Current Plaky placement."
                    ),
                ),
                StructuredTool.from_function(
                    coroutine=_plaky_link_prs,
                    name="plaky_link_prs",
                    description=(
                        "Link one or more GitHub PR URLs to an existing Plaky task/item by adding a PR links comment. "
                        "Uses the same formatting and Plaky comment route as the CLI `link-pr`. "
                        "Args: task_id, pr_urls (string containing one or more URLs), optional board_id/placement."
                    ),
                ),
                StructuredTool.from_function(
                    coroutine=_plaky_create_subtask,
                    name="plaky_create_subtask",
                    description=(
                        "Create a subtask on parent_task_id with workflow/assignment/repo fields. "
                        "Args: parent_task_id, title, description, priority, status, task_type, repo_tag, "
                        "engineer_plaky_id, qa_plaky_id, auto_assign_qa, optional board_id, optional group_id."
                    ),
                ),
            ]
        )
    return tools
