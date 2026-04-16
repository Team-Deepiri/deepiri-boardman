"""LangChain tools wrapping PlakyClient (async)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from langchain_core.tools import StructuredTool

from boardman.plaky.board_schema import (
    fetch_board_schema_bundle,
    validate_field_values_against_board_schema,
)
from boardman.plaky.client import PlakyClient
from boardman.plaky.name_match import rank_plaky_rows


def _normalize_task_title(raw: str) -> tuple[str, Optional[str]]:
    t = (raw or "").strip()
    if not t:
        return "", "title must be non-empty"
    if len(t) > 160:
        return "", "title must be <= 160 characters"
    return t, None


def _match_option_value(options: List[str], value: Any) -> tuple[Optional[str], Optional[str]]:
    if not options or not isinstance(value, str):
        return value if value is None or isinstance(value, str) else str(value), None
    v = value.strip()
    if not v:
        return "", None
    by_casefold = {str(opt).strip().casefold(): str(opt).strip() for opt in options if str(opt).strip()}
    hit = by_casefold.get(v.casefold())
    if hit is not None:
        return hit, None
    return None, f"value {value!r} not in allowed options: {options[:20]}"


async def _validate_field_values_against_schema(
    board_id: str,
    values: Dict[str, Any],
) -> tuple[Dict[str, Any], List[str], List[str]]:
    bid = (board_id or "").strip()
    if not bid or not values:
        return values, [], []
    bundle = await fetch_board_schema_bundle(bid)
    normalized = bundle.get("normalized") or {}
    fields = normalized.get("fields") if isinstance(normalized, dict) else []
    if not isinstance(fields, list) or not fields:
        return values, [], ["board schema had no field definitions; skipped key/value validation"]

    by_key: Dict[str, Dict[str, Any]] = {}
    for f in fields:
        if not isinstance(f, dict):
            continue
        key = str(f.get("key") or "").strip()
        if key:
            by_key[key] = f

    if not by_key:
        return values, [], ["board schema had no field keys; skipped key/value validation"]

    cleaned: Dict[str, Any] = {}
    errors: List[str] = []
    warnings: List[str] = []
    for k, v in values.items():
        ks = str(k).strip()
        if not ks:
            continue
        field = by_key.get(ks)
        if field is None:
            errors.append(f"unknown field key {ks!r} for board {bid}")
            continue
        opts = field.get("options") if isinstance(field.get("options"), list) else []
        matched, err = _match_option_value([str(x) for x in opts if str(x).strip()], v)
        if err:
            errors.append(f"{ks}: {err}")
            continue
        cleaned[ks] = matched

    if bundle.get("ok") is not True:
        warnings.append(f"schema bundle returned warning: {bundle.get('message') or 'unknown'}")
    return cleaned, errors, warnings


async def _validate_status_priority_with_schema(
    board_id: str,
    status: Optional[str],
    priority: Optional[str],
) -> tuple[Optional[str], Optional[str], List[str]]:
    bid = (board_id or "").strip()
    if not bid:
        return status, priority, []
    bundle = await fetch_board_schema_bundle(bid)
    fields = (bundle.get("normalized") or {}).get("fields") or []
    if not isinstance(fields, list):
        return status, priority, []
    status_opts: List[str] = []
    priority_opts: List[str] = []
    for f in fields:
        if not isinstance(f, dict):
            continue
        nm = str(f.get("name") or "").strip().casefold()
        opts = [str(x).strip() for x in (f.get("options") or []) if str(x).strip()]
        if nm in {"status", "state"} and opts:
            status_opts = opts
        if nm == "priority" and opts:
            priority_opts = opts
    errors: List[str] = []
    new_status = status
    new_priority = priority
    if status is not None and status_opts:
        matched, err = _match_option_value(status_opts, status)
        if err:
            errors.append(f"status: {err}")
        else:
            new_status = matched
    if priority is not None and priority_opts:
        matched, err = _match_option_value(priority_opts, priority)
        if err:
            errors.append(f"priority: {err}")
        else:
            new_priority = matched
    return new_status, new_priority, errors


async def _plaky_list_boards() -> str:
    """Return all boards (id + name) from Plaky — use when placement is unset or user asks what exists."""
    c = PlakyClient()
    raw = await c.list_boards()
    return json.dumps(raw, default=str)[:12000]


async def _plaky_list_tasks(status: str = "open") -> str:
    c = PlakyClient()
    r = await c.get_tasks(status=status)
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
        {"list_ok": raw.get("ok"), "message": raw.get("message"), "matches": matches[:25], "best": best},
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
        {"list_ok": raw.get("ok"), "message": raw.get("message"), "matches": matches[:25], "best": best},
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


def _field_text(item: Dict[str, Any], *keys: str) -> str:
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
    """
    Diagnose board organization quality (duplicates, stale-ish items, missing acceptance criteria).
    Read-only helper for REVIEW mode before any write action.
    """
    from boardman.agent.tool_context import get_context_plaky_board_id, get_context_plaky_group_id

    bid = (board_id or "").strip() or (get_context_plaky_board_id() or "")
    gid = (group_id or "").strip() or (get_context_plaky_group_id() or "")
    if not bid:
        return json.dumps({"ok": False, "message": "board_id missing (pass arg or set current placement)"})
    c = PlakyClient()
    lim = max(1, min(int(max_items or 200), 600))
    raw = await c.list_board_items(bid, max_pages=max(1, (lim // 100) + 1))
    if not raw.get("ok"):
        return json.dumps({"ok": False, "message": raw.get("message") or "Could not load board items"})
    items = raw.get("items") or []
    if not isinstance(items, list):
        items = []
    if gid:
        filtered: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            g = it.get("group") if isinstance(it.get("group"), dict) else {}
            candidate = str(it.get("groupId") or it.get("group_id") or g.get("id") or "").strip()
            if candidate == gid:
                filtered.append(it)
        items = filtered
    items = [it for it in items if isinstance(it, dict)][:lim]

    by_title: Dict[str, List[Dict[str, Any]]] = {}
    missing_acceptance: List[Dict[str, Any]] = []
    stale_candidates: List[Dict[str, Any]] = []
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
        updated = _field_text(it, "updatedAt", "updated_at", "lastUpdatedAt", "createdAt", "created_at")
        if updated and ("2023" in updated or "2024" in updated):
            stale_candidates.append({"id": item_id, "title": title, "updated": updated, "status": status})

    duplicate_clusters = [
        {"title_key": k, "items": vals}
        for k, vals in by_title.items()
        if len(vals) > 1 and k not in {"", "task"}
    ]
    duplicate_clusters = sorted(duplicate_clusters, key=lambda x: len(x["items"]), reverse=True)[:20]

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
    priority: str = "medium",
    repo_tag: str = "",
    board_id: str = "",
    group_id: str = "",
    field_values_json: str = "",
) -> str:
    from boardman.agent.task_draft import load_task_draft, merge_draft_into_field_values
    from boardman.agent.tool_context import (
        get_agent_session_pk,
        get_context_plaky_board_id,
        get_tool_db_session,
    )

    c = PlakyClient()
    title_norm, title_err = _normalize_task_title(title)
    if title_err:
        return json.dumps({"ok": False, "message": f"invalid task title: {title_err}"})
    full = f"[{repo_tag}] {title_norm}" if repo_tag else title_norm
    bid = board_id.strip() or None
    gid = group_id.strip() or None
    effective_board_id = bid or get_context_plaky_board_id() or ""

    parsed: Dict[str, Any] = {}
    raw_f = (field_values_json or "").strip()
    if raw_f:
        try:
            loaded = json.loads(raw_f)
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "message": "field_values_json must be valid JSON object"})
        if not isinstance(loaded, dict):
            return json.dumps({"ok": False, "message": "field_values_json must be a JSON object of fieldKey -> value"})
        parsed = loaded

    db = get_tool_db_session()
    pk = get_agent_session_pk()
    if db is not None and pk is not None:
        draft = await load_task_draft(db, pk)
        merged = merge_draft_into_field_values(draft, parsed)
    else:
        merged = dict(parsed)

    validated_fv: Dict[str, Any] = dict(merged)
    fv_errors: List[str] = []
    fv_warnings: List[str] = []
    if validated_fv and effective_board_id:
        validated_fv, fv_errors, fv_warnings = await _validate_field_values_against_schema(
            effective_board_id,
            validated_fv,
        )
    if fv_errors:
        return json.dumps(
            {
                "ok": False,
                "message": "field_values_json contains invalid keys/values for board schema",
                "errors": fv_errors,
                "warnings": fv_warnings,
            },
            default=str,
        )

    fv = validated_fv if validated_fv else None
    r = await c.create_task(
        title=full,
        description=description,
        priority=priority,
        board_id=bid,
        group_id=gid,
        field_values=fv,
    )
    out = dict(r) if isinstance(r, dict) else {"result": r}
    if fv:
        out["merged_field_values"] = fv
    if fv_warnings:
        out["field_validation_warnings"] = fv_warnings
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
    if parsed:
        bundle = await fetch_board_schema_bundle(bid)
        normalized = bundle.get("normalized") if isinstance(bundle.get("normalized"), dict) else None
        err = validate_field_values_against_board_schema(parsed, normalized)
        if err:
            return json.dumps({"ok": False, "message": err}, default=str)
    c = PlakyClient()
    r = await c.patch_item_field_values(bid, item_id.strip(), parsed)
    return json.dumps(r, default=str)


async def _plaky_update_task(
    task_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    priority: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
    from boardman.agent.tool_context import get_context_plaky_board_id

    title_err = None
    if title is not None:
        _, title_err = _normalize_task_title(title)
    if title_err:
        return json.dumps({"ok": False, "message": f"invalid task title: {title_err}"})
    board_id = get_context_plaky_board_id() or ""
    status_norm, priority_norm, errors = await _validate_status_priority_with_schema(
        board_id,
        status,
        priority,
    )
    if errors:
        return json.dumps(
            {
                "ok": False,
                "message": "status/priority not valid for current board schema",
                "errors": errors,
            }
        )
    c = PlakyClient()
    r = await c.update_task_fields(
        task_id,
        title=title,
        description=description,
        priority=priority_norm,
        status=status_norm,
    )
    return json.dumps(r, default=str)


async def _plaky_add_comment(task_id: str, body: str) -> str:
    c = PlakyClient()
    r = await c.add_comment(task_id, body)
    return json.dumps(r, default=str)


async def _plaky_create_subtask(parent_task_id: str, title: str, description: str = "") -> str:
    c = PlakyClient()
    r = await c.create_subtask(parent_task_id, title, description)
    return json.dumps(r, default=str)


def build_plaky_tools(*, allow_writes: bool) -> List[StructuredTool]:
    tools: List[StructuredTool] = [
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
            description="List Plaky tasks. Args: status (open|done|... default open).",
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
            coroutine=_plaky_review_board,
            name="plaky_review_board",
            description=(
                "Read-only board diagnosis: scan items for duplicates, stale candidates, and missing acceptance criteria. "
                "Args: optional board_id, optional group_id, optional max_items."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_plaky_save_task_preferences,
            name="plaky_save_task_preferences",
            description=(
                "Save assignee + Plaky field defaults for **this chat session** (persists in DB). "
                "Args: preferences_json — JSON with field_values {fieldKey: value}, optional "
                "engineer_plaky_id, qa_plaky_id (mapped via team_assignments Plaky keys), summary, "
                "replace_field_values (bool). Next **plaky_create_task** merges these automatically."
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
                        "Args: title, description, priority, repo_tag?, board_id?, group_id?, field_values_json?."
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
                        "Patch a Plaky task. Args: task_id, optional title, description, priority, status."
                    ),
                ),
                StructuredTool.from_function(
                    coroutine=_plaky_add_comment,
                    name="plaky_add_comment",
                    description="Add a comment to a Plaky task. Args: task_id, body (markdown).",
                ),
                StructuredTool.from_function(
                    coroutine=_plaky_create_subtask,
                    name="plaky_create_subtask",
                    description=(
                        "Create a subtask or subtask comment on parent_task_id. "
                        "Args: parent_task_id, title, description."
                    ),
                ),
            ]
        )
    return tools
