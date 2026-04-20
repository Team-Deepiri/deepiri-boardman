import logging
from typing import Any, List, Optional, Set

from fastapi import APIRouter, Depends
from pydantic import AliasChoices, BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.agent.tool_context import get_context_plaky_board_id, get_context_plaky_group_id
from boardman.assignment.config import (
    infer_plaky_field_keys_from_normalized,
    load_team_assignments,
    sync_team_assignment_field_keys_from_board,
)
from boardman.assignment.qa_picker import (
    build_repo_field_map,
    pick_engineer_for_repo,
    pick_qa_for_repo,
)
from boardman.database.session import get_db
from boardman.plaky.client import PlakyClient
from boardman.plaky.placement import plaky_placement_context
from boardman.plaky.board_schema import (
    fetch_board_schema_bundle,
    field_likely_person_column,
    field_row_item_key,
    looks_like_placeholder_plaky_field_key,
    plaky_field_row_label,
    plaky_repo_field_value_format,
    resolve_repo_tag_field_values_from_schema,
    select_field_patch_pair_from_schema,
)
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


router = APIRouter()
_log = logging.getLogger(__name__)


def _allowed_item_field_keys_from_schema(schema_normalized: Optional[dict]) -> Set[str]:
    """Real `itemFieldKey` values from the loaded board schema (includes stubs merged from items)."""
    out: Set[str] = set()
    if not isinstance(schema_normalized, dict):
        return out
    for f in schema_normalized.get("fields") or []:
        if isinstance(f, dict):
            k = field_row_item_key(f)
            if k:
                out.add(k)
    return out


def _scrub_placeholder_field_key(key: str, *, allowed_board_keys: Set[str]) -> str:
    """
    Drop YAML/env keys that match the LLM-placeholder *pattern* but are **not** on this board.

    Native Plaky boards often use ids like `person-1` / `status-2`; those appear in `allowed_board_keys`
    from schema and must be kept.
    """
    k = (key or "").strip()
    if not k:
        return ""
    if k in allowed_board_keys:
        return k
    if looks_like_placeholder_plaky_field_key(k):
        return ""
    return k


def _person_item_field_keys_from_normalized(schema_normalized: Optional[dict]) -> Set[str]:
    """Plaky itemFieldKeys for columns that look like person/assignee (used for two-phase PATCH)."""
    out: Set[str] = set()
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
    extra_repos: Optional[List[str]],
    filters: dict[str, Any],
) -> List[str]:
    """Ordered unique owner/repo strings from request + filters."""
    out: List[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        t = (s or "").strip()
        if not t:
            return
        k = t.lower()
        if k in seen:
            return
        seen.add(k)
        out.append(t)

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
    normalized: Optional[dict] = None,
) -> tuple[str, str]:
    """
    When team_assignments.yml omits engineer/QA Plaky field keys, infer PERSON columns from the board schema.
    Returns (engineer_key, qa_key) — only fills slots that were empty.
    """
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
        if not qa and person_fields:
            qa = person_fields[0][0]
        if not eng:
            for k, _ in person_fields:
                if k != qa:
                    eng = k
                    break
        if len(person_fields) >= 2:
            if not eng:
                eng = person_fields[0][0]
            if not qa:
                qa = next((p[0] for p in person_fields if p[0] != eng), person_fields[1][0])
    except Exception:
        pass
    return eng, qa


def _extract_created_task_id(result: dict) -> str:
    task = result.get("task") if isinstance(result, dict) and isinstance(result.get("task"), dict) else {}
    candidates = [
        result.get("task_id") if isinstance(result, dict) else None,
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
    person_field_keys: Optional[Set[str]] = None,
) -> dict:
    """
    Inspect the JSON body of POST /tasks: this object is returned as `post_create_assignment`.
    It includes `field_values_attempted` (what we sent to Plaky PATCH …/items/{id}/fields) and
    the `patch_item_field_values` result (`ok`, `mode`, `failed` with HTTP snippets on error).
    On successful patch, `board_item` is a fresh GET of the item (create-time `task` is not updated).
    """
    if not field_values:
        return {"ok": True, "skipped": True, "message": "No assignment fields provided"}
    if not board_id:
        return {"ok": False, "skipped": True, "message": "Cannot patch assignments without board_id"}

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
                rid = str(row.get("id") or row.get("itemId") or row.get("taskId") or row.get("_id") or "").strip()
                if not rid:
                    continue
                row_group = str(
                    row.get("groupId")
                    or row.get("group_id")
                    or ((row.get("group") or {}).get("id") if isinstance(row.get("group"), dict) else "")
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
                        row.get("id") or row.get("itemId") or row.get("taskId") or row.get("_id") or ""
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

    patched = await plaky.patch_item_field_values(board_id, item_id, field_values, person_field_keys=person_field_keys)
    if isinstance(patched, dict):
        patched["item_id"] = item_id
        patched["item_id_source"] = id_source
        patched["field_values_attempted"] = dict(field_values)
        if patched.get("ok"):
            try:
                refreshed = await plaky.get_board_item_public(board_id.strip(), item_id.strip())
                if refreshed.get("ok") and isinstance(refreshed.get("item"), dict):
                    # Create response `task` is not updated after PATCH; this is the item as Plaky serves it now.
                    patched["board_item"] = refreshed["item"]
            except Exception:
                pass
    return patched if isinstance(patched, dict) else {"ok": False, "message": "Unexpected patch response"}


class CreateTaskRequest(BaseModel):
    title: str
    description: str = ""
    priority: str = "Medium"
    status: str = "In Progress"
    task_type: str = Field(
        default="Feature",
        validation_alias=AliasChoices("type", "task_type"),
    )
    repo: Optional[str] = None
    github_repos: Optional[List[str]] = None  # more owner/repo strings; merged with repo, deduped
    plaky_board_id: Optional[str] = None
    plaky_group_id: Optional[str] = None
    engineer_plaky_id: Optional[str] = None
    qa_plaky_id: Optional[str] = None
    # When True (default), empty engineer/QA in the request are filled from team_assignments.yml.
    # Explicit engineer_plaky_id / qa_plaky_id always win over roster picks for that slot.
    auto_assign_team: bool = True
    filters: Optional[dict] = None


def _http_placement_ids(req: CreateTaskRequest) -> tuple[str, str]:
    """Resolve Plaky board/group for POST /tasks: body, env defaults, then agent chat context."""
    board = (req.plaky_board_id or "").strip()
    if not board:
        board = (settings.plaky_default_board_id or "").strip()
    if not board:
        board = (get_context_plaky_board_id() or "").strip()
    group = (req.plaky_group_id or "").strip()
    if not group:
        group = (settings.plaky_default_group_id or "").strip()
    if not group:
        group = (get_context_plaky_group_id() or "").strip()
    return board, group


def _board_id_from_create_result(result: Optional[dict]) -> str:
    """When the HTTP handler did not have board_id, recover it from the Plaky create response."""
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


class LinkPRRequest(BaseModel):
    pr_url: str
    task_id: str
    update_status: bool = False


@router.post("/tasks")
async def create_task(req: CreateTaskRequest, session: AsyncSession = Depends(get_db)):
    plaky = PlakyClient()
    filters = req.filters if isinstance(req.filters, dict) else {}

    raw_title = (req.title or "").strip() or str(filters.get("title") or "").strip()
    raw_description = (req.description or "").strip() or str(filters.get("description") or "").strip()
    raw_status = (req.status or "").strip() or str(filters.get("status") or "").strip()
    raw_task_type = (req.task_type or "").strip() or str(
        filters.get("type") or filters.get("task_type") or ""
    ).strip()
    raw_priority = (req.priority or "").strip() or str(filters.get("priority") or "").strip()
    canon_status = canonical_task_status(raw_status)
    canon_type = canonical_task_type(raw_task_type)
    canon_priority = canonical_task_priority(raw_priority)
    engineer_plaky_id = (req.engineer_plaky_id or "").strip() or str(filters.get("engineer_plaky_id") or "").strip()
    qa_plaky_id = (req.qa_plaky_id or "").strip() or str(filters.get("qa_plaky_id") or "").strip()

    if not raw_title:
        return {"ok": False, "status": 400, "message": "title is required"}

    title = raw_title
    primary_repo = (req.repo or "").strip() or str(filters.get("repo") or "").strip()
    merged_repos = _merge_github_repo_inputs(
        primary_repo=primary_repo,
        extra_repos=req.github_repos,
        filters=filters,
    )
    repo_full = merged_repos[0] if merged_repos else (primary_repo or "deepiri-org/unknown")
    repo_display = merged_repos[0] if merged_repos else repo_full

    qa_field_fallback = (settings.plaky_qa_item_field_key or "").strip()

    effective_board_id, effective_group_id = _http_placement_ids(req)

    # Board items require board + group; if group is missing, use the first group on the board.
    if effective_board_id and not effective_group_id:
        try:
            gr = await plaky.list_groups(effective_board_id)
            groups = gr.get("groups") if isinstance(gr, dict) else []
            if isinstance(groups, list) and groups:
                gid0 = str(groups[0].get("id") or "").strip() if isinstance(groups[0], dict) else ""
                if gid0:
                    effective_group_id = gid0
        except Exception:
            pass

    schema_normalized: Optional[dict] = None
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
    scrubbed_placeholder_keys: List[str] = []
    raw_eng = (cfg.plaky_field_engineer or "").strip()
    raw_qa = (cfg.plaky_field_qa or "").strip()
    raw_repo = (cfg.plaky_field_repo or "").strip()
    raw_repos_multi = (cfg.plaky_field_github_repos or "").strip()
    for label, raw in (
        ("engineer", raw_eng),
        ("qa", raw_qa),
        ("repo", raw_repo),
        ("github_repos", raw_repos_multi),
        ("plaky_qa_item_field_key", (qa_field_fallback or "").strip()),
    ):
        if (
            raw
            and looks_like_placeholder_plaky_field_key(raw)
            and raw not in allowed_board_keys
        ):
            scrubbed_placeholder_keys.append(f"{label}:{raw}")
    cfg_engineer_key = _scrub_placeholder_field_key(raw_eng, allowed_board_keys=allowed_board_keys)
    cfg_qa_key = _scrub_placeholder_field_key(raw_qa, allowed_board_keys=allowed_board_keys)
    cfg_repo_key = _scrub_placeholder_field_key(raw_repo, allowed_board_keys=allowed_board_keys)
    cfg_github_repos_key = _scrub_placeholder_field_key(raw_repos_multi, allowed_board_keys=allowed_board_keys)
    qa_env_fallback = _scrub_placeholder_field_key(
        (qa_field_fallback or "").strip(), allowed_board_keys=allowed_board_keys
    )

    engineer_field_key = cfg_engineer_key
    qa_field_key = cfg_qa_key or qa_env_fallback

    pick_eng_id, pick_eng_reason = pick_engineer_for_repo(repo_full, cfg)
    pick_qa_id, pick_qa_reason = await pick_qa_for_repo(repo_full, cfg)

    # Primary: YAML keys. Fallback: infer PERSON columns from board when keys are missing.
    needs_infer = effective_board_id and (
        (engineer_plaky_id and not engineer_field_key)
        or (qa_plaky_id and not qa_field_key)
        or (not cfg_engineer_key or not cfg_qa_key)
        or (req.auto_assign_team and bool(str(pick_eng_id or "").strip()) and not engineer_field_key)
        or (req.auto_assign_team and bool(str(pick_qa_id or "").strip()) and not qa_field_key)
    )
    if needs_infer:
        engineer_field_key, qa_field_key = await _infer_plaky_person_column_keys(
            effective_board_id,
            engineer_field_key,
            qa_field_key,
            normalized=schema_normalized,
        )

    engineer_field_key = _scrub_placeholder_field_key(
        engineer_field_key, allowed_board_keys=allowed_board_keys
    )
    qa_field_key = _scrub_placeholder_field_key(qa_field_key, allowed_board_keys=allowed_board_keys)

    repo_plaky_key = (
        cfg_repo_key or inferred_from_schema.get("repo") or ""
    ).strip()
    github_repos_plaky_key = (
        cfg_github_repos_key or inferred_from_schema.get("github_repos") or ""
    ).strip()
    repo_plaky_key = _scrub_placeholder_field_key(repo_plaky_key, allowed_board_keys=allowed_board_keys)
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
    # Per-slot: request contributor/QA ids override roster; empty request uses roster when auto_assign_team.
    eng_apply = engineer_plaky_id or (pick_eng_id if req.auto_assign_team else "")
    qa_apply = qa_plaky_id or (pick_qa_id if req.auto_assign_team else "")
    if eng_apply and engineer_field_key:
        field_values[engineer_field_key] = str(eng_apply).strip()
    if qa_apply and qa_field_key:
        field_values[qa_field_key] = str(qa_apply).strip()

    pri = plaky_create_legacy_priority_param(canon_priority)
    priority_labels = priority_field_patch_candidates(canon_priority)

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
            value_label_candidates=priority_labels,
        ),
    ):
        if pair:
            fk, fv = pair
            if fk and fv is not None and fk not in field_values:
                field_values[fk] = fv

    tag_resolution_warnings: List[dict] = []
    tag_keys = {k.strip() for k in (repo_plaky_key, github_repos_plaky_key) if (k or "").strip()}
    if tag_keys and isinstance(schema_normalized, dict):
        _, tag_resolution_warnings = resolve_repo_tag_field_values_from_schema(
            field_values, schema_normalized, keys=tag_keys
        )

    schema_sample_labels: List[str] = []
    if isinstance(schema_normalized, dict):
        raw_fields = schema_normalized.get("fields") or []
        if isinstance(raw_fields, list):
            for f in raw_fields[:20]:
                if isinstance(f, dict):
                    lab = plaky_field_row_label(f) or field_row_item_key(f)
                    if lab:
                        schema_sample_labels.append(lab)

    person_keys = _person_item_field_keys_from_normalized(schema_normalized)

    assignment_inspect: dict[str, Any] = {
        "effective_board_id": effective_board_id or None,
        "effective_group_id": effective_group_id or None,
        "repo_full": repo_full,
        "repo_display": repo_display,
        "merged_repos": merged_repos,
        "auto_assign_team": req.auto_assign_team,
        "member_count": len(cfg.members),
        "yaml_plaky_field_engineer": cfg_engineer_key or None,
        "yaml_plaky_field_qa": cfg_qa_key or None,
        "scrubbed_placeholder_field_keys": scrubbed_placeholder_keys or None,
        "allowed_board_field_keys_count": len(allowed_board_keys),
        "resolved_engineer_field_key": (engineer_field_key or None),
        "resolved_qa_field_key": (qa_field_key or None),
        "applied_engineer_plaky_id": (str(eng_apply).strip() if eng_apply else None),
        "applied_qa_plaky_id": (str(qa_apply).strip() if qa_apply else None),
        "engineer_source": ("request" if engineer_plaky_id else ("roster" if eng_apply else None)),
        "qa_source": ("request" if qa_plaky_id else ("roster" if qa_apply else None)),
        "repo_plaky_key": repo_plaky_key or None,
        "github_repos_plaky_key": github_repos_plaky_key or None,
        "inferred_from_schema": dict(inferred_from_schema),
        "schema_sample_field_labels": schema_sample_labels,
        "pick_engineer_plaky_id": pick_eng_id,
        "pick_engineer_reason": pick_eng_reason,
        "pick_qa_plaky_id": pick_qa_id,
        "pick_qa_reason": pick_qa_reason,
        "task_status": canon_status,
        "task_type": canon_type,
        "task_priority": canon_priority,
        "field_value_keys": sorted(field_values.keys()),
        "field_values": dict(field_values),
        "person_field_keys_for_patch": sorted(person_keys) if person_keys else [],
        "tag_option_resolution_warnings": tag_resolution_warnings or None,
    }

    async with plaky_placement_context(
        effective_board_id or None,
        effective_group_id or None,
    ):
        result = await plaky.create_task(
            title=title,
            description=raw_description,
            priority=pri,
            board_id=effective_board_id or None,
            group_id=effective_group_id or None,
            field_values=field_values or None,
            person_field_keys=person_keys or None,
            # HTTP handler always runs `_run_post_create_assignments`; avoid patching twice (noisy in Plaky).
            defer_field_patch=bool(field_values),
        )

    if not result.get("ok"):
        if isinstance(result, dict):
            result["assignment_inspect"] = assignment_inspect
        _log.info("POST /tasks assignment_inspect=%s", assignment_inspect)
        return result

    patch_board_id = (effective_board_id or "").strip() or _board_id_from_create_result(result)
    assignment_inspect["patch_board_id"] = patch_board_id or None
    _log.info("POST /tasks assignment_inspect=%s", assignment_inspect)

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
    result["assignment_inspect"] = assignment_inspect

    return result


@router.get("/tasks")
async def list_tasks(status: str = "open", session: AsyncSession = Depends(get_db)):
    plaky = PlakyClient()
    result = await plaky.get_tasks(status=status)
    return result


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, session: AsyncSession = Depends(get_db)):
    plaky = PlakyClient()
    result = await plaky.get_task(task_id)
    return result


@router.post("/tasks/{task_id}/link-pr")
async def link_pr(task_id: str, req: LinkPRRequest, session: AsyncSession = Depends(get_db)):
    plaky = PlakyClient()
    comment = f"**PR Linked:** [View PR]({req.pr_url})"
    result = await plaky.add_comment(task_id, comment)

    if not result.get("ok"):
        return result

    if req.update_status:
        from boardman.settings import settings
        await plaky.update_task_status(task_id, settings.plaky_pr_merge_status)

    return result
