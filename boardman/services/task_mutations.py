from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boardman.agent.tool_context import get_context_plaky_board_id, get_context_plaky_group_id
from boardman.assignment.config import (
    infer_plaky_field_keys_from_normalized,
    load_team_assignments,
    sync_team_assignment_field_keys_from_board,
)
from boardman.assignment.developer_eligibility import filter_developer
from boardman.assignment.qa_picker import (
    build_repo_field_map,
    ensure_github_owner_repo,
    pick_qa_for_repo,
)
from boardman.plaky.board_aware import resolve_group_for_repo
from boardman.plaky.board_schema import (
    fetch_board_schema_bundle,
    field_likely_person_column,
    field_row_item_key,
    looks_like_placeholder_plaky_field_key,
    plaky_field_row_label,
    plaky_item_field_value,
    plaky_item_person_ids,
    plaky_repo_field_value_format,
    resolve_repo_tag_field_values_from_schema,
    select_field_patch_pair_from_schema,
)
from boardman.plaky.client import PlakyClient
from boardman.plaky.placement import plaky_placement_context
from boardman.plaky.task_payload_ids import board_id_from_plaky_task as _board_id_from_task_payload
from boardman.plaky.task_tag_vocab import (
    canonical_task_priority,
    canonical_task_status,
    canonical_task_type,
    plaky_create_legacy_priority_param,
    priority_field_patch_candidates,
    status_field_patch_candidates,
    type_field_patch_candidates,
)
from boardman.settings import settings


@dataclass(slots=True)
class CreateTaskInput:
    title: str
    description: str = ""
    priority: str = "Medium"
    # "" = caller expressed no preference; status then follows the assignee
    # (engineer -> Assigned, none -> NEEDS ASSIGNED) instead of a hardcoded default.
    status: str = ""
    task_type: str = "Feature"
    repo: str | None = None
    github_repos: list[str] | None = None
    plaky_board_id: str | None = None
    plaky_group_id: str | None = None
    engineer_plaky_id: str | None = None
    qa_plaky_id: str | None = None
    auto_assign_team: bool = True
    filters: dict[str, Any] | None = None
    field_values: dict[str, Any] | None = None


@dataclass(slots=True)
class UpdateTaskInput:
    """Partial updates only: workflow fields and QA / engineer assignee."""

    status: str | None = None
    # With status: optional board field key (e.g. option id from resolve_plaky_status_patch).
    status_plaky_field_key: str | None = None
    task_type: str | None = None
    priority: str | None = None
    title: str | None = None
    description: str | None = None
    qa_plaky_id: str | None = None
    # Engineer/developer assignee fill-in (PR author → Plaky user id). Field key is resolved
    # per-board when not supplied.
    engineer_plaky_id: str | None = None
    engineer_plaky_field_key: str | None = None
    clear_engineer_assignee: bool = False
    # Read the current Plaky item/task when possible and omit fields already equal
    # to the desired value.  If the read is unavailable, the safe fallback is the
    # existing write behavior so a transient read outage does not lose a sync.
    diff_only: bool = False
    auto_assign_qa: bool = False
    github_repo: str | None = None
    plaky_board_id: str | None = None


@dataclass(slots=True)
class CreateSubtaskInput:
    parent_task_id: str
    title: str
    description: str = ""
    priority: str = "Medium"
    status: str = ""  # "" = follow the assignee, same as CreateTaskInput
    task_type: str = "Feature"
    github_repos: list[str] | None = None
    engineer_plaky_id: str | None = None
    qa_plaky_id: str | None = None
    auto_assign_qa: bool = True
    plaky_board_id: str | None = None
    plaky_group_id: str | None = None


def _allowed_item_field_keys_from_schema(schema_normalized: dict | None) -> set[str]:
    out: set[str] = set()
    if not isinstance(schema_normalized, dict):
        return out
    for f in schema_normalized.get("fields") or []:
        if isinstance(f, dict):
            k = field_row_item_key(f)
            if k:
                out.add(k)
    return out


def _scrub_placeholder_field_key(key: str, *, allowed_board_keys: set[str]) -> str:
    k = (key or "").strip()
    if not k:
        return ""
    if k in allowed_board_keys:
        return k
    if looks_like_placeholder_plaky_field_key(k):
        return ""
    # Board schema is known — silently drop keys the board doesn't have
    if allowed_board_keys:
        return ""
    return k


def _person_item_field_keys_from_normalized(schema_normalized: dict | None) -> set[str]:
    out: set[str] = set()
    if not isinstance(schema_normalized, dict):
        return out
    raw_fields = schema_normalized.get("fields") or []
    if not isinstance(raw_fields, list):
        return out
    for f in raw_fields:
        if isinstance(f, dict) and field_likely_person_column(f):
            k = field_row_item_key(f)
            if k:
                out.add(k)
    return out


def _merge_github_repo_inputs(
    *,
    primary_repo: str,
    extra_repos: list[str] | None,
    filters: dict[str, Any],
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        raw = (s or "").strip()
        if not raw:
            return
        # Accept either repeated values or one combined value:
        # "owner/a owner/b", "owner/a,owner/b", or newline-separated.
        tokens = [
            p.strip()
            for p in raw.replace("\n", ",").replace("\t", " ").replace(",", " ").split(" ")
            if p.strip()
        ]
        for t in tokens:
            canon = ensure_github_owner_repo(t)
            if not canon:
                continue
            k = canon.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(canon)

    add(primary_repo)
    if isinstance(extra_repos, list):
        for x in extra_repos:
            add(str(x))
    gr = filters.get("github_repos")
    if isinstance(gr, list):
        for x in gr:
            add(str(x))
    elif isinstance(gr, str) and gr.strip():
        for part in gr.replace("\n", ",").split(","):
            add(part)
    return out


async def _infer_plaky_person_column_keys(
    board_id: str,
    engineer_field_key: str,
    qa_field_key: str,
    *,
    normalized: dict | None = None,
) -> tuple[str, str]:
    eng = (engineer_field_key or "").strip()
    qa = (qa_field_key or "").strip()
    bid = (board_id or "").strip()
    if not bid or (eng and qa):
        return eng, qa
    try:
        if normalized is None:
            bundle = await fetch_board_schema_bundle(bid)
            normalized = bundle.get("normalized") if isinstance(bundle, dict) else None
        fields = normalized.get("fields") if isinstance(normalized, dict) else []
        person_fields: list[tuple[str, str]] = []
        if isinstance(fields, list):
            for f in fields:
                if not isinstance(f, dict):
                    continue
                key = field_row_item_key(f)
                name = plaky_field_row_label(f)
                if key and field_likely_person_column(f):
                    person_fields.append((key, name))
        if not person_fields:
            return eng, qa
        if not qa:
            for k, n in person_fields:
                if "qa" in n or "quality" in n:
                    qa = k
                    break
        if not eng:
            for k, n in person_fields:
                if k == qa:
                    continue
                if any(
                    tok in n
                    for tok in ("engineer", "developer", "dev", "contributor", "owner", "assignee")
                ):
                    eng = k
                    break
        # No column NAMED qa/quality: do not fall back to the first person column —
        # that is almost always the Assignee, and writing the QA pick there hands the
        # task's ownership to the reviewer. An unwritable QA is an honest skip.
        if not eng:
            for k, _ in person_fields:
                if k != qa:
                    eng = k
                    break
    except Exception:
        pass
    return eng, qa


def _extract_created_task_id(result: dict) -> str:
    task = (
        result.get("task")
        if isinstance(result, dict) and isinstance(result.get("task"), dict)
        else {}
    )
    candidates = [
        result.get("task_id"),
        task.get("id"),
        task.get("itemId"),
        task.get("taskId"),
        task.get("_id"),
    ]
    for raw in candidates:
        val = str(raw or "").strip()
        if val:
            return val
    for key in ("item", "data", "result", "task"):
        nested = task.get(key)
        if isinstance(nested, dict):
            for nk in ("id", "itemId", "taskId", "_id"):
                val = str(nested.get(nk) or "").strip()
                if val:
                    return val
    return ""


async def _run_post_create_assignments(
    plaky: PlakyClient,
    *,
    result: dict,
    board_id: str,
    group_id: str,
    title: str,
    field_values: dict[str, Any],
    person_field_keys: set[str] | None = None,
) -> dict:
    if not field_values:
        return {"ok": True, "skipped": True, "message": "No assignment fields provided"}
    if not board_id:
        return {
            "ok": False,
            "skipped": True,
            "message": "Cannot patch assignments without board_id",
        }

    item_id = _extract_created_task_id(result)
    id_source = "create_response" if item_id else ""
    if not item_id:
        listed = await plaky.list_board_items(board_id, max_pages=2)
        rows = listed.get("items") if isinstance(listed, dict) else []
        if isinstance(rows, list):
            title_norm = title.strip().lower()
            group_norm = group_id.strip()
            for row in reversed(rows):
                if not isinstance(row, dict):
                    continue
                rid = str(
                    row.get("id") or row.get("itemId") or row.get("taskId") or row.get("_id") or ""
                ).strip()
                if not rid:
                    continue
                row_group = str(
                    row.get("groupId")
                    or row.get("group_id")
                    or (
                        (row.get("group") or {}).get("id")
                        if isinstance(row.get("group"), dict)
                        else ""
                    )
                    or ""
                ).strip()
                row_title = str(row.get("name") or row.get("title") or "").strip().lower()
                if group_norm and row_group and row_group != group_norm:
                    continue
                if title_norm and row_title and title_norm not in row_title:
                    continue
                item_id = rid
                id_source = "list_match_title_group"
                break
            if not item_id:
                for row in reversed(rows):
                    if not isinstance(row, dict):
                        continue
                    rid = str(
                        row.get("id")
                        or row.get("itemId")
                        or row.get("taskId")
                        or row.get("_id")
                        or ""
                    ).strip()
                    if rid:
                        item_id = rid
                        id_source = "list_latest_fallback"
                        break

    if not item_id:
        return {
            "ok": False,
            "message": "Task created but post-create assignment could not resolve item id",
            "attempted_fields": sorted(field_values.keys()),
            "field_values_attempted": dict(field_values),
        }

    patched = await plaky.patch_item_field_values(
        board_id, item_id, field_values, person_field_keys=person_field_keys
    )
    if isinstance(patched, dict):
        patched["item_id"] = item_id
        patched["item_id_source"] = id_source
        patched["field_values_attempted"] = dict(field_values)
        if patched.get("ok"):
            try:
                refreshed = await plaky.get_board_item_public(board_id.strip(), item_id.strip())
                if refreshed.get("ok") and isinstance(refreshed.get("item"), dict):
                    patched["board_item"] = refreshed["item"]
            except Exception:
                pass
    return (
        patched
        if isinstance(patched, dict)
        else {"ok": False, "message": "Unexpected patch response"}
    )


def _http_placement_ids(req: CreateTaskInput) -> tuple[str, str]:
    board = (req.plaky_board_id or "").strip()
    if not board:
        board = (get_context_plaky_board_id() or "").strip()
    group = (req.plaky_group_id or "").strip()
    if not group:
        group = (get_context_plaky_group_id() or "").strip()
    return board, group


def _board_id_from_create_result(result: dict | None) -> str:
    if not isinstance(result, dict):
        return ""
    for top in (result.get("task"), result):
        if not isinstance(top, dict):
            continue
        for k in ("boardId", "board_id"):
            v = top.get(k)
            if isinstance(v, dict):
                v = v.get("id") or v.get("boardId")
            if v is not None and str(v).strip():
                return str(v).strip()
        board = top.get("board")
        if isinstance(board, dict):
            v = board.get("id") or board.get("boardId")
            if v is not None and str(v).strip():
                return str(v).strip()
    return ""


def _schema_field_maps(
    schema_normalized: dict | None,
) -> tuple[dict[str, str], dict[str, dict[Any, str]]]:
    labels: dict[str, str] = {}
    option_labels: dict[str, dict[Any, str]] = {}
    if not isinstance(schema_normalized, dict):
        return labels, option_labels
    raw_fields = schema_normalized.get("fields") or []
    if not isinstance(raw_fields, list):
        return labels, option_labels
    for f in raw_fields:
        if not isinstance(f, dict):
            continue
        key = field_row_item_key(f)
        if not key:
            continue
        label = plaky_field_row_label(f) or key
        labels[key] = label
        opts = f.get("options") or []
        if not isinstance(opts, list):
            continue
        by_val: dict[Any, str] = {}
        for opt in opts:
            if not isinstance(opt, dict):
                continue
            lab = str(opt.get("name") or "").strip()
            if not lab:
                continue
            for k in ("id", "optionId", "value", "_id", "name"):
                raw = opt.get(k)
                if raw is None:
                    continue
                by_val[raw] = lab
                by_val[str(raw)] = lab
        if by_val:
            option_labels[key] = by_val
    return labels, option_labels


def _value_for_comment(value: Any, option_map: dict[Any, str] | None = None) -> str:
    if value is None:
        return "(empty)"
    if option_map:
        if value in option_map:
            return option_map[value]
        sv = str(value)
        if sv in option_map:
            return option_map[sv]
    if isinstance(value, dict):
        for k in ("name", "label", "text", "value"):
            v = value.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        for k in ("users", "assignedUsers", "tagValues", "tags"):
            v = value.get(k)
            if isinstance(v, list) and v:
                return ", ".join(_value_for_comment(x, option_map) for x in v)
        if "id" in value:
            return str(value.get("id"))
        return str(value)
    if isinstance(value, list):
        if not value:
            return "(empty)"
        return ", ".join(_value_for_comment(x, option_map) for x in value)
    return str(value)


async def _status_follow_assignee(
    board_id: str,
    field_values: dict[str, Any],
    *,
    engineer_written: bool,
    explicit_status: bool,
) -> dict[str, Any] | None:
    """Make Status agree with the Assignee at write time.

    Employer rule: "if there's an Assignee in the column, its status is Assigned".
    A task was being created WITH a developer and still said NEEDS ASSIGNED - the two
    columns contradicted each other on the board and nobody's workflow moved.

    - engineer written, status not explicitly requested -> Assigned
    - engineer written, status explicitly the pre-assignment state -> Assigned wins
      (asking for both an assignee and NEEDS ASSIGNED is a contradiction; the person
      is the stronger signal)
    - no engineer, no explicit status -> NEEDS ASSIGNED (creation default)
    - any other explicit status -> left alone; this never downgrades In Progress etc.
    """
    from boardman.plaky.dynamic_qa_status import resolve_plaky_status_patch

    bid = (board_id or "").strip()
    if not bid:
        return None
    assigned_rp = await resolve_plaky_status_patch(bid, intent="workflow_assigned")
    needs_rp = await resolve_plaky_status_patch(bid, intent="workflow_needs_assigned")
    target = assigned_rp if engineer_written else needs_rp
    if not target:
        return None
    sk, sv = target
    needs_vals = set()
    if needs_rp:
        needs_vals.add(str(needs_rp[1]).casefold())
    needs_vals.add("needs assigned")

    cur = field_values.get(sk)
    if not explicit_status:
        # Whatever sits in field_values here came from the canonical DEFAULT ladder,
        # not from the caller — the assignee-derived status always wins over a default.
        field_values[sk] = sv
        return {"status_follows_assignee": {"field": sk, "value": sv}}
    if engineer_written and cur is not None and str(cur).casefold() in needs_vals:
        field_values[sk] = sv
        return {"status_follows_assignee": {"field": sk, "value": sv, "overrode": "needs assigned"}}
    return None


async def bump_status_for_assignee(
    board_id: str, task_id: str, plaky: PlakyClient | None = None
) -> dict[str, Any] | None:
    """If the task currently sits at the pre-assignment status, move it to Assigned.

    For paths that write an engineer onto an EXISTING task (update, raw field patch):
    read-then-bump, never blind-write — a task already In Progress or deeper must not
    be dragged back. Returns the patch result, or None when no move was needed.
    """
    from boardman.plaky.board_schema import plaky_item_status_id
    from boardman.plaky.dynamic_qa_status import resolve_plaky_status_patch

    bid = (board_id or "").strip()
    tid = (task_id or "").strip()
    if not bid or not tid:
        return None
    assigned_rp = await resolve_plaky_status_patch(bid, intent="workflow_assigned")
    if not assigned_rp:
        return None
    needs_rp = await resolve_plaky_status_patch(bid, intent="workflow_needs_assigned")
    client = plaky or PlakyClient()
    info = await client.get_board_item_public(bid, tid)
    item = info.get("item") if isinstance(info, dict) else None
    if not item:
        return None
    sk, sv = assigned_rp
    cur = str(plaky_item_status_id(item, sk) or "")
    needs_val = str(needs_rp[1]) if needs_rp else ""
    if cur and cur != needs_val:
        return None  # already past NEEDS ASSIGNED — never downgrade
    res = await client.patch_item_field_values(bid, tid, {sk: sv})
    return {"status_follows_assignee": {"field": sk, "value": sv, "ok": res.get("ok")}}


async def create_task_internal(req: CreateTaskInput) -> dict[str, Any]:
    plaky = PlakyClient()
    filters = req.filters if isinstance(req.filters, dict) else {}

    raw_title = (req.title or "").strip() or str(filters.get("title") or "").strip()
    raw_description = (req.description or "").strip() or str(
        filters.get("description") or ""
    ).strip()
    raw_status = (req.status or "").strip() or str(filters.get("status") or "").strip()
    raw_task_type = (req.task_type or "").strip() or str(
        filters.get("type") or filters.get("task_type") or ""
    ).strip()
    raw_priority = (req.priority or "").strip() or str(filters.get("priority") or "").strip()
    canon_status = canonical_task_status(raw_status)
    canon_type = canonical_task_type(raw_task_type)
    canon_priority = canonical_task_priority(raw_priority)
    engineer_plaky_id = (req.engineer_plaky_id or "").strip() or str(
        filters.get("engineer_plaky_id") or ""
    ).strip()
    qa_plaky_id = (req.qa_plaky_id or "").strip() or str(filters.get("qa_plaky_id") or "").strip()
    if not raw_title:
        return {"ok": False, "status": 400, "message": "title is required"}

    title = raw_title
    primary_repo = str(filters.get("repo") or "").strip()
    merged_repos = _merge_github_repo_inputs(
        primary_repo=primary_repo, extra_repos=req.github_repos, filters=filters
    )
    repo_full = merged_repos[0] if merged_repos else ""
    repo_display = repo_full
    qa_field_fallback = (settings.plaky_qa_item_field_key or "").strip()
    effective_board_id, effective_group_id = _http_placement_ids(req)

    if effective_board_id and not effective_group_id:
        try:
            repo_short = repo_full.rsplit("/", 1)[-1] if repo_full else ""
            resolved = await resolve_group_for_repo(
                effective_board_id, repo_short, fallback_group_id=None, plaky=plaky
            )
            if resolved:
                effective_group_id = resolved
            else:
                gr = await plaky.list_groups(effective_board_id)
                groups = gr.get("groups") if isinstance(gr, dict) else []
                if isinstance(groups, list) and groups:
                    gid0 = (
                        str(groups[0].get("id") or "").strip()
                        if isinstance(groups[0], dict)
                        else ""
                    )
                    if gid0:
                        effective_group_id = gid0
        except Exception:
            pass

    schema_normalized: dict[str, Any] | None = None
    inferred_from_schema: dict[str, str] = {}
    if effective_board_id:
        try:
            await sync_team_assignment_field_keys_from_board(effective_board_id)
        except Exception:
            pass
        try:
            bundle = await fetch_board_schema_bundle(effective_board_id)
            sn = bundle.get("normalized") if isinstance(bundle, dict) else None
            if isinstance(sn, dict):
                schema_normalized = sn
                inferred_from_schema = infer_plaky_field_keys_from_normalized(sn)
        except Exception:
            pass

    allowed_board_keys = _allowed_item_field_keys_from_schema(schema_normalized)
    cfg = load_team_assignments()
    cfg_engineer_key = _scrub_placeholder_field_key(
        (cfg.plaky_field_engineer or "").strip(), allowed_board_keys=allowed_board_keys
    )
    cfg_qa_key = _scrub_placeholder_field_key(
        (cfg.plaky_field_qa or "").strip(), allowed_board_keys=allowed_board_keys
    )
    cfg_repo_key = _scrub_placeholder_field_key(
        (cfg.plaky_field_repo or "").strip(), allowed_board_keys=allowed_board_keys
    )
    cfg_github_repos_key = _scrub_placeholder_field_key(
        (cfg.plaky_field_github_repos or "").strip(), allowed_board_keys=allowed_board_keys
    )
    qa_env_fallback = _scrub_placeholder_field_key(
        (qa_field_fallback or "").strip(), allowed_board_keys=allowed_board_keys
    )
    # Board schema wins over global config: category boards use different person
    # keys per board (e.g. Assignee is person-2 on one board, person-4 on another),
    # and a global key can exist on the target board while naming the wrong column.
    engineer_field_key = inferred_from_schema.get("engineer") or cfg_engineer_key
    qa_field_key = inferred_from_schema.get("qa") or cfg_qa_key or qa_env_fallback

    # Only rank QA when the result can actually be used. This ran unconditionally, so
    # every create paid repo tier classification plus GitHub-fit scoring (45s budget)
    # and threw the answer away — 20 wasted passes on a 20-row batch, since QA is
    # assigned when a PR opens, not at creation.
    pick_qa_id, pick_qa_reason = "", "qa auto-assign off (QA is picked when a PR opens)"
    if req.auto_assign_team and not qa_plaky_id:
        pick_qa_id, pick_qa_reason = await pick_qa_for_repo(repo_full, cfg)
    needs_infer = effective_board_id and (
        (engineer_plaky_id and not engineer_field_key)
        or (qa_plaky_id and not qa_field_key)
        or (not cfg_engineer_key or not cfg_qa_key)
        or (req.auto_assign_team and bool(str(pick_qa_id or "").strip()) and not qa_field_key)
    )
    if needs_infer:
        engineer_field_key, qa_field_key = await _infer_plaky_person_column_keys(
            effective_board_id, engineer_field_key, qa_field_key, normalized=schema_normalized
        )
    engineer_field_key = _scrub_placeholder_field_key(
        engineer_field_key, allowed_board_keys=allowed_board_keys
    )
    qa_field_key = _scrub_placeholder_field_key(qa_field_key, allowed_board_keys=allowed_board_keys)

    repo_plaky_key = (cfg_repo_key or inferred_from_schema.get("repo") or "").strip()
    github_repos_plaky_key = (
        cfg_github_repos_key or inferred_from_schema.get("github_repos") or ""
    ).strip()
    repo_plaky_key = _scrub_placeholder_field_key(
        repo_plaky_key, allowed_board_keys=allowed_board_keys
    )
    github_repos_plaky_key = _scrub_placeholder_field_key(
        github_repos_plaky_key, allowed_board_keys=allowed_board_keys
    )

    repo_val_fmt = plaky_repo_field_value_format(schema_normalized, repo_plaky_key)
    gh_repos_val_fmt = plaky_repo_field_value_format(schema_normalized, github_repos_plaky_key)
    rk_set, gk_set = (repo_plaky_key or "").strip(), (github_repos_plaky_key or "").strip()
    if rk_set == gk_set and rk_set and (repo_val_fmt == "short" or gh_repos_val_fmt == "short"):
        repo_val_fmt = gh_repos_val_fmt = "short"

    field_values = build_repo_field_map(
        cfg,
        repo_value=repo_display,
        github_repos=merged_repos if merged_repos else None,
        plaky_field_repo_key=repo_plaky_key or None,
        plaky_field_github_repos_key=github_repos_plaky_key or None,
        repo_value_format=repo_val_fmt,
        github_repos_value_format=gh_repos_val_fmt,
    )
    eng_apply, eng_refusal = filter_developer(engineer_plaky_id)
    qa_apply = qa_plaky_id or (pick_qa_id if req.auto_assign_team else "")
    if eng_apply and engineer_field_key:
        field_values[engineer_field_key] = str(eng_apply).strip()
    if qa_apply and qa_field_key:
        field_values[qa_field_key] = str(qa_apply).strip()
    if isinstance(req.field_values, dict) and req.field_values:
        field_values.update(req.field_values)
    # Whatever route the person came in by - resolved argument, raw field_values from
    # the HTTP API, or a name the assistant matched - a QA-only or excluded account may
    # not end up in the developer column.
    if engineer_field_key and field_values.get(engineer_field_key):
        kept, refusal = filter_developer(str(field_values[engineer_field_key]))
        if not kept:
            field_values.pop(engineer_field_key, None)
            eng_refusal = eng_refusal or refusal

    pri = plaky_create_legacy_priority_param(canon_priority)
    for pair in (
        select_field_patch_pair_from_schema(
            schema_normalized,
            column_name_substrings=("status", "state", "workflow"),
            value_label_candidates=status_field_patch_candidates(canon_status),
        ),
        select_field_patch_pair_from_schema(
            schema_normalized,
            column_name_substrings=("type", "issue type", "category", "kind"),
            value_label_candidates=type_field_patch_candidates(canon_type),
            exclude_name_substrings=("subtype",),
        ),
        select_field_patch_pair_from_schema(
            schema_normalized,
            column_name_substrings=("priority", "prio"),
            value_label_candidates=priority_field_patch_candidates(canon_priority),
        ),
    ):
        if pair:
            fk, fv = pair
            if fk and fv is not None and fk not in field_values:
                field_values[fk] = fv

    # Read the assignee off the FINAL merged map, not off eng_apply. The assistant can
    # only reach the person column through req.field_values, which is merged above at
    # line 694 — after eng_apply was computed. Judging by eng_apply alone made every
    # assistant-created task claim "nobody is assigned" while a person sat in the
    # Assignee column, and the board then read NEEDS ASSIGNED. Reproduced live: task
    # 7185894, assignee 481106, status NEEDS ASSIGNED.
    assignee_present = bool(engineer_field_key and str(field_values.get(engineer_field_key) or ""))
    # Type and Priority are ALSO "status-N" columns on Plaky boards (Bots: Status
    # status-8, Type status-7, Priority status-9), so a prefix test treated setting a
    # priority as "the user chose a status" and suppressed the default. Compare against
    # the one key the status ladder actually resolved.
    # Identify the status COLUMN itself. Resolving it through a value lookup would make
    # this depend on the board happening to have an "In Progress"-like option, and a
    # board without one would silently overwrite a caller's chosen status.
    status_keys: set[str] = set()
    if isinstance(schema_normalized, dict):
        for f in schema_normalized.get("fields") or []:
            if not isinstance(f, dict):
                continue
            name = plaky_field_row_label(f)
            key = field_row_item_key(f)
            if not key or not name:
                continue
            if any(tok in name for tok in ("status", "state", "workflow")):
                status_keys.add(key)
    status_follow_note = await _status_follow_assignee(
        effective_board_id,
        field_values,
        engineer_written=assignee_present,
        explicit_status=bool(
            raw_status
            or (isinstance(req.field_values, dict) and (set(req.field_values) & status_keys))
        ),
    )

    tag_keys = {k.strip() for k in (repo_plaky_key, github_repos_plaky_key) if (k or "").strip()}
    tag_resolution_warnings: list[dict[str, Any]] = []
    if tag_keys and isinstance(schema_normalized, dict):
        _, tag_resolution_warnings = resolve_repo_tag_field_values_from_schema(
            field_values, schema_normalized, keys=tag_keys
        )

    person_keys = _person_item_field_keys_from_normalized(schema_normalized)
    async with plaky_placement_context(effective_board_id or None, effective_group_id or None):
        result = await plaky.create_task(
            title=title,
            description=raw_description,
            priority=pri,
            board_id=effective_board_id or None,
            group_id=effective_group_id or None,
            field_values=field_values or None,
            person_field_keys=person_keys or None,
            defer_field_patch=False,
        )
    if not result.get("ok"):
        return result
    if status_follow_note:
        result.update(status_follow_note)

    explicit_board_id = (req.plaky_board_id or "").strip()
    created_board_id = _board_id_from_create_result(result)
    patch_board_id = explicit_board_id or created_board_id or (effective_board_id or "").strip()
    field_patch = result.get("field_patch") if isinstance(result, dict) else None
    field_patch_ok = isinstance(field_patch, dict) and bool(field_patch.get("ok"))
    if field_values and field_patch_ok:
        post_assign = dict(field_patch)
    else:
        post_assign = await _run_post_create_assignments(
            plaky,
            result=result,
            board_id=patch_board_id,
            group_id=effective_group_id,
            title=title,
            field_values=field_values,
            person_field_keys=person_keys or None,
        )
    result["post_create_assignment"] = post_assign
    if eng_refusal:
        # The caller asked for a developer who is not eligible. Say so instead of
        # quietly creating the task with an empty Assignee.
        result["developer_not_assigned"] = eng_refusal
    if tag_resolution_warnings:
        result["tag_resolution_warnings"] = tag_resolution_warnings

    # The status was decided from what we INTENDED to write. If the person column did
    # not actually take (an id Plaky rejects, e.g. a workspace user who is not on this
    # board), the task would read Assigned with an empty Assignee — the state the board
    # rules forbid. Verify the column and walk the status back when it is empty.
    if assignee_present and patch_board_id and status_follow_note:
        created_task_id = str(
            (result.get("task") or {}).get("id")
            or (result.get("task") or {}).get("taskId")
            or result.get("task_id")
            or ""
        ).strip()
        # The task already exists at this point. Nothing here may raise, or the batch
        # path would render a created task as a failed row.
        try:
            if created_task_id:
                from boardman.plaky.board_schema import plaky_item_person_ids
                from boardman.plaky.dynamic_qa_status import resolve_plaky_status_patch

                info = await plaky.get_board_item_public(patch_board_id, created_task_id)
                read_ok = bool(info.get("ok")) and bool(info.get("item"))
                if not read_ok:
                    # A failed read is NOT evidence the column is empty. Downgrading on
                    # a transient 429 would create the very contradiction this guards.
                    result["assignee_unverified"] = (
                        "could not re-read the item to confirm the assignee; status left as written"
                    )
                elif not plaky_item_person_ids(info["item"], engineer_field_key):
                    rp = await resolve_plaky_status_patch(
                        patch_board_id, intent="workflow_needs_assigned"
                    )
                    if rp:
                        await plaky.patch_item_field_values(
                            patch_board_id, created_task_id, {rp[0]: rp[1]}
                        )
                        result["assignee_did_not_apply"] = (
                            f"{engineer_field_key} stayed empty, so the status was set back "
                            "to NEEDS ASSIGNED instead of claiming an owner"
                        )
        except Exception as exc:  # noqa: BLE001 - never fail a created task on a check
            result["assignee_unverified"] = f"assignee verification skipped: {exc}"[:200]
    return result


async def create_subtask_internal(req: CreateSubtaskInput) -> dict[str, Any]:
    plaky = PlakyClient()

    parent_task_id = (req.parent_task_id or "").strip()
    title = (req.title or "").strip()
    description = (req.description or "").strip()
    board_id = (req.plaky_board_id or "").strip() or (get_context_plaky_board_id() or "").strip()
    group_id = (req.plaky_group_id or "").strip() or (get_context_plaky_group_id() or "").strip()
    canon_status = canonical_task_status((req.status or "").strip())
    canon_type = canonical_task_type((req.task_type or "").strip())
    canon_priority = canonical_task_priority((req.priority or "").strip())
    engineer_plaky_id = (req.engineer_plaky_id or "").strip()
    qa_plaky_id = (req.qa_plaky_id or "").strip()

    if not parent_task_id:
        return {"ok": False, "status": 400, "message": "parent_task_id is required"}
    if not title:
        return {"ok": False, "status": 400, "message": "title is required"}
    merged_repos = _merge_github_repo_inputs(
        primary_repo="", extra_repos=req.github_repos, filters={}
    )
    repo_full = merged_repos[0] if merged_repos else ""

    schema_normalized: dict[str, Any] | None = None
    inferred_from_schema: dict[str, str] = {}
    if board_id:
        try:
            await sync_team_assignment_field_keys_from_board(board_id)
        except Exception:
            pass
        try:
            bundle = await fetch_board_schema_bundle(board_id)
            sn = bundle.get("normalized") if isinstance(bundle, dict) else None
            if isinstance(sn, dict):
                schema_normalized = sn
                inferred_from_schema = infer_plaky_field_keys_from_normalized(sn)
        except Exception:
            pass

    allowed_board_keys = _allowed_item_field_keys_from_schema(schema_normalized)
    cfg = load_team_assignments()
    cfg_engineer_key = _scrub_placeholder_field_key(
        (cfg.plaky_field_engineer or "").strip(), allowed_board_keys=allowed_board_keys
    )
    cfg_qa_key = _scrub_placeholder_field_key(
        (cfg.plaky_field_qa or "").strip(), allowed_board_keys=allowed_board_keys
    )
    cfg_repo_key = _scrub_placeholder_field_key(
        (cfg.plaky_field_repo or "").strip(), allowed_board_keys=allowed_board_keys
    )
    cfg_github_repos_key = _scrub_placeholder_field_key(
        (cfg.plaky_field_github_repos or "").strip(), allowed_board_keys=allowed_board_keys
    )

    engineer_field_key = inferred_from_schema.get("engineer") or cfg_engineer_key
    qa_field_key = inferred_from_schema.get("qa") or cfg_qa_key
    pick_qa_id, pick_qa_reason = await pick_qa_for_repo(repo_full, cfg)
    needs_infer = board_id and (
        (engineer_plaky_id and not engineer_field_key)
        or (qa_plaky_id and not qa_field_key)
        or (req.auto_assign_qa and bool(str(pick_qa_id or "").strip()) and not qa_field_key)
    )
    if needs_infer:
        engineer_field_key, qa_field_key = await _infer_plaky_person_column_keys(
            board_id, engineer_field_key, qa_field_key, normalized=schema_normalized
        )
    engineer_field_key = _scrub_placeholder_field_key(
        engineer_field_key, allowed_board_keys=allowed_board_keys
    )
    qa_field_key = _scrub_placeholder_field_key(qa_field_key, allowed_board_keys=allowed_board_keys)

    repo_plaky_key = (cfg_repo_key or inferred_from_schema.get("repo") or "").strip()
    github_repos_plaky_key = (
        cfg_github_repos_key or inferred_from_schema.get("github_repos") or ""
    ).strip()
    repo_plaky_key = _scrub_placeholder_field_key(
        repo_plaky_key, allowed_board_keys=allowed_board_keys
    )
    github_repos_plaky_key = _scrub_placeholder_field_key(
        github_repos_plaky_key, allowed_board_keys=allowed_board_keys
    )
    repo_val_fmt = plaky_repo_field_value_format(schema_normalized, repo_plaky_key)
    gh_repos_val_fmt = plaky_repo_field_value_format(schema_normalized, github_repos_plaky_key)

    field_values = build_repo_field_map(
        cfg,
        repo_value=repo_full,
        github_repos=merged_repos if merged_repos else None,
        plaky_field_repo_key=repo_plaky_key or None,
        plaky_field_github_repos_key=github_repos_plaky_key or None,
        repo_value_format=repo_val_fmt,
        github_repos_value_format=gh_repos_val_fmt,
    )
    qa_apply = qa_plaky_id or (pick_qa_id if req.auto_assign_qa else "")
    if engineer_plaky_id and engineer_field_key:
        field_values[engineer_field_key] = engineer_plaky_id
    if qa_apply and qa_field_key:
        field_values[qa_field_key] = qa_apply

    for pair in (
        select_field_patch_pair_from_schema(
            schema_normalized,
            column_name_substrings=("status", "state", "workflow"),
            value_label_candidates=status_field_patch_candidates(canon_status),
        ),
        select_field_patch_pair_from_schema(
            schema_normalized,
            column_name_substrings=("type", "issue type", "category", "kind"),
            value_label_candidates=type_field_patch_candidates(canon_type),
            exclude_name_substrings=("subtype",),
        ),
        select_field_patch_pair_from_schema(
            schema_normalized,
            column_name_substrings=("priority", "prio"),
            value_label_candidates=priority_field_patch_candidates(canon_priority),
        ),
    ):
        if pair:
            fk, fv = pair
            if fk and fv is not None and fk not in field_values:
                field_values[fk] = fv

    await _status_follow_assignee(
        board_id,
        field_values,
        engineer_written=bool(engineer_plaky_id and engineer_field_key),
        explicit_status=bool((req.status or "").strip()),
    )

    person_keys = _person_item_field_keys_from_normalized(schema_normalized)
    pri = plaky_create_legacy_priority_param(canon_priority)

    async with plaky_placement_context(board_id or None, group_id or None):
        result = await plaky.create_subtask(
            parent_task_id=parent_task_id,
            title=title,
            description=description,
            status=canon_status,
            task_type=canon_type,
            priority=pri,
            field_values=field_values or None,
            person_field_keys=person_keys or None,
            board_id=board_id or None,
            group_id=group_id or None,
        )
    if isinstance(result, dict):
        result.setdefault("parent_task_id", parent_task_id)
    return result


async def update_task_internal(task_id: str, req: UpdateTaskInput) -> dict[str, Any]:
    plaky = PlakyClient()
    ops: dict[str, Any] = {}
    developer_refusals: list[str] = []

    update_status_raw = (req.status or "").strip()
    update_status_plaky_key_raw = (req.status_plaky_field_key or "").strip()
    update_type_raw = (req.task_type or "").strip()
    update_priority_raw = (req.priority or "").strip()
    update_title = req.title if req.title is not None else None
    update_description = req.description if req.description is not None else None
    update_qa = (req.qa_plaky_id or "").strip()
    update_engineer = (req.engineer_plaky_id or "").strip()
    clear_engineer = bool(req.clear_engineer_assignee)
    update_engineer_key_raw = (req.engineer_plaky_field_key or "").strip()
    update_repo_in = (req.github_repo or "").strip()
    auto_assign_qa = bool(req.auto_assign_qa)

    if auto_assign_qa and not update_qa:
        if not update_repo_in:
            return {
                "ok": False,
                "status": 400,
                "message": "github_repo is required when auto_assign_qa is enabled and qa_plaky_id is not provided",
            }
        repo_for_pick = ensure_github_owner_repo(update_repo_in)
        cfg_for_pick = load_team_assignments()
        picked_qa, picked_reason = await pick_qa_for_repo(repo_for_pick, cfg_for_pick)
        qa_ops: dict[str, Any] = {
            "ok": bool((picked_qa or "").strip()),
            "repo": repo_for_pick,
            "picked_qa_plaky_id": picked_qa,
            "reason": picked_reason,
        }
        if repo_for_pick.casefold() != update_repo_in.casefold():
            qa_ops["repo_input"] = update_repo_in
        ops["qa_auto_assign"] = qa_ops
        if not picked_qa:
            return {
                "ok": False,
                "status": 400,
                "message": (
                    f"Could not auto-assign QA for repo '{repo_for_pick}'"
                    f" (from '{update_repo_in}'): {picked_reason}"
                    if repo_for_pick.casefold() != update_repo_in.casefold()
                    else f"Could not auto-assign QA for repo '{repo_for_pick}': {picked_reason}"
                ),
                "operations": ops,
            }
        update_qa = str(picked_qa).strip()

    wants_board_patch = any(
        (
            update_status_raw,
            update_type_raw,
            update_priority_raw,
            update_qa,
            update_engineer,
            clear_engineer,
            auto_assign_qa,
        )
    )
    wants_text_patch = update_title is not None or update_description is not None

    board_id = (req.plaky_board_id or "").strip()
    needs_board_lookup = wants_board_patch or wants_text_patch
    if needs_board_lookup and not board_id:
        got = await plaky.get_task(task_id)
        task = (
            got.get("task") if isinstance(got, dict) and isinstance(got.get("task"), dict) else {}
        )
        board_id = _board_id_from_task_payload(task)
    if needs_board_lookup and not board_id:
        board_id = (get_context_plaky_board_id() or "").strip()

    status_added_to_field_values = False

    if wants_board_patch and board_id:
        schema_normalized: dict[str, Any] | None = None
        try:
            await sync_team_assignment_field_keys_from_board(board_id)
        except Exception:
            pass
        try:
            bundle = await fetch_board_schema_bundle(board_id)
            sn = bundle.get("normalized") if isinstance(bundle, dict) else None
            if isinstance(sn, dict):
                schema_normalized = sn
        except Exception:
            pass

        allowed_board_keys = _allowed_item_field_keys_from_schema(schema_normalized)
        cfg = load_team_assignments()
        qa_field_key = _scrub_placeholder_field_key(
            (cfg.plaky_field_qa or "").strip(), allowed_board_keys=allowed_board_keys
        )
        engineer_field_key = _scrub_placeholder_field_key(
            update_engineer_key_raw or (cfg.plaky_field_engineer or "").strip(),
            allowed_board_keys=allowed_board_keys,
        )
        engineer_field_key, qa_field_key = await _infer_plaky_person_column_keys(
            board_id, engineer_field_key, qa_field_key, normalized=schema_normalized
        )
        qa_field_key = _scrub_placeholder_field_key(
            qa_field_key, allowed_board_keys=allowed_board_keys
        )
        engineer_field_key = _scrub_placeholder_field_key(
            engineer_field_key, allowed_board_keys=allowed_board_keys
        )

        field_values: dict[str, Any] = {}
        if update_qa and qa_field_key:
            field_values[qa_field_key] = update_qa
        if update_engineer and engineer_field_key:
            update_engineer, upd_refusal = filter_developer(update_engineer)
            if update_engineer:
                field_values[engineer_field_key] = update_engineer
            elif upd_refusal:
                developer_refusals.append(upd_refusal)
        elif clear_engineer and engineer_field_key:
            field_values[engineer_field_key] = ""

        canon_status = canonical_task_status(update_status_raw) if update_status_raw else ""
        canon_type = canonical_task_type(update_type_raw) if update_type_raw else ""
        canon_priority = canonical_task_priority(update_priority_raw) if update_priority_raw else ""

        direct_status_key = _scrub_placeholder_field_key(
            update_status_plaky_key_raw, allowed_board_keys=allowed_board_keys
        )
        if update_status_raw and direct_status_key:
            field_values[direct_status_key] = update_status_raw
            status_added_to_field_values = True

        status_pair = None
        if canon_status and not status_added_to_field_values:
            status_pair = select_field_patch_pair_from_schema(
                schema_normalized,
                column_name_substrings=("status", "state", "workflow"),
                value_label_candidates=status_field_patch_candidates(canon_status),
            )
        pairs = (
            status_pair,
            (
                select_field_patch_pair_from_schema(
                    schema_normalized,
                    column_name_substrings=("type", "issue type", "category", "kind"),
                    value_label_candidates=type_field_patch_candidates(canon_type),
                    exclude_name_substrings=("subtype",),
                )
                if canon_type
                else None
            ),
            (
                select_field_patch_pair_from_schema(
                    schema_normalized,
                    column_name_substrings=("priority", "prio"),
                    value_label_candidates=priority_field_patch_candidates(canon_priority),
                )
                if canon_priority
                else None
            ),
        )
        for pair in pairs:
            if pair:
                fk, fv = pair
                if fk and fv is not None:
                    field_values[fk] = fv
                    if pair is status_pair:
                        status_added_to_field_values = True

        if req.diff_only and field_values:
            try:
                current_info = await plaky.get_board_item_public(board_id, task_id)
                current_item = current_info.get("item") if current_info.get("ok") else None
                if isinstance(current_item, dict):
                    person_keys = _person_item_field_keys_from_normalized(schema_normalized)

                    def same_value(key: str, desired: Any) -> bool:
                        if key in person_keys:
                            actual_ids = plaky_item_person_ids(current_item, key)
                            desired_ids = [] if desired in (None, "") else [str(desired).strip()]
                            return actual_ids == desired_ids
                        actual = plaky_item_field_value(current_item, key)
                        if isinstance(actual, dict):
                            actual = actual.get("id") or actual.get("value") or actual.get("key")
                        if isinstance(desired, dict):
                            desired = (
                                desired.get("id") or desired.get("value") or desired.get("key")
                            )
                        return (
                            str(actual or "").strip().casefold()
                            == str(desired or "").strip().casefold()
                        )

                    before = dict(field_values)
                    field_values = {
                        key: value
                        for key, value in field_values.items()
                        if not same_value(str(key), value)
                    }
                    if update_status_raw and status_pair and status_pair[0] not in field_values:
                        status_added_to_field_values = True
                    ops["field_diff"] = {
                        "ok": True,
                        "requested": list(before),
                        "changed": list(field_values),
                        "skipped": [key for key in before if key not in field_values],
                    }
            except Exception as e:
                ops["field_diff"] = {"ok": False, "fallback": True, "message": str(e)[:200]}

        if field_values:
            person_keys = _person_item_field_keys_from_normalized(schema_normalized)
            patch = await plaky.patch_item_field_values(
                board_id, task_id, field_values, person_field_keys=person_keys or None
            )
            patch["field_values_attempted"] = dict(field_values)
            ops["field_patch"] = patch
        else:
            ops["field_patch"] = {
                "ok": True,
                "skipped": True,
                "message": "No board fields requested",
            }
    elif wants_board_patch:
        ops["field_patch"] = {
            "ok": False,
            "message": "Board id required for QA/status/type/priority updates",
        }

    if update_status_raw and not status_added_to_field_values:
        legacy = await plaky.update_task_fields(task_id, status=update_status_raw)
        ops["legacy_task_fields"] = legacy

    if wants_text_patch:
        text_updates: dict[str, str] = {}
        if req.diff_only:
            try:
                current_task_result = await plaky.get_task(task_id)
                current_task = (
                    current_task_result.get("task") if current_task_result.get("ok") else None
                )
                if isinstance(current_task, dict):
                    current_title = str(current_task.get("title") or current_task.get("name") or "")
                    current_description = str(current_task.get("description") or "")
                    if update_title is not None and current_title != update_title:
                        text_updates["title"] = update_title
                    if update_description is not None and current_description != update_description:
                        text_updates["description"] = update_description
                    ops["text_diff"] = {
                        "ok": True,
                        "changed": list(text_updates),
                        "skipped": [
                            key
                            for key, value in (
                                ("title", update_title),
                                ("description", update_description),
                            )
                            if value is not None and key not in text_updates
                        ],
                    }
                else:
                    text_updates = {
                        key: value
                        for key, value in (
                            ("title", update_title),
                            ("description", update_description),
                        )
                        if value is not None
                    }
            except Exception as e:
                text_updates = {
                    key: value
                    for key, value in (("title", update_title), ("description", update_description))
                    if value is not None
                }
                ops["text_diff"] = {"ok": False, "fallback": True, "message": str(e)[:200]}
        else:
            text_updates = {
                key: value
                for key, value in (("title", update_title), ("description", update_description))
                if value is not None
            }
        if text_updates:
            if board_id and callable(getattr(plaky, "update_item_text", None)):
                item_text = await plaky.update_item_text(
                    board_id,
                    task_id,
                    title=text_updates.get("title"),
                    description=text_updates.get("description"),
                )
                ops["item_text_fields"] = item_text
            else:
                legacy_text = await plaky.update_task_fields(
                    task_id,
                    title=text_updates.get("title"),
                    description=text_updates.get("description"),
                )
                ops["legacy_text_fields"] = legacy_text

    # An engineer landed on the task but the caller asked for no status: make Status
    # agree with the Assignee (NEEDS ASSIGNED -> Assigned; deeper statuses untouched).
    if update_engineer and not update_status_raw and board_id:
        try:
            bumped = await bump_status_for_assignee(board_id, task_id, plaky)
            if bumped:
                ops.update(bumped)
        except Exception as e:
            ops["status_follows_assignee"] = {"ok": False, "message": str(e)[:200]}

    requested_any = wants_board_patch or wants_text_patch
    if not requested_any:
        return {"ok": False, "status": 400, "message": "No update fields provided"}
    op_results = [v for v in ops.values() if isinstance(v, dict)]
    ok = bool(op_results) and all(bool(v.get("ok")) for v in op_results if "ok" in v)
    out: dict[str, Any] = {"ok": ok, "task_id": task_id, "operations": ops}
    if developer_refusals:
        # The requested developer was not eligible; the column was left alone rather
        # than filled with a QA-only or excluded account.
        out["developer_not_assigned"] = developer_refusals
    return out
