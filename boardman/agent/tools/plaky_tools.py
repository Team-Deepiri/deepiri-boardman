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
from boardman.plaky.field_coercion import coerce_field_values
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


def _slim_task(t: dict) -> dict:
    """Project a Plaky item down to what a PM actually needs, so a full board fits."""
    if not isinstance(t, dict):
        return {}
    out = {
        "id": t.get("id") or t.get("taskId"),
        "title": t.get("title") or t.get("name"),
        "status": t.get("status"),
    }
    people: list = []
    for f in t.get("fields") or []:
        if not isinstance(f, dict):
            continue
        if f.get("type") == "PERSON":
            v = f.get("value") or {}
            users = v.get("assignedUsers") if isinstance(v, dict) else None
            if users:
                people.append({"field": f.get("title") or f.get("key"), "users": users})
        elif f.get("type") == "STATUS" and not out.get("status"):
            out["status"] = f.get("value")
    if people:
        out["assignees"] = people
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def _envelope(payload: dict, items: list, *, limit: int = 60) -> str:
    """Serialize a list result with an explicit count envelope.

    A raw ``json.dumps(...)[:12000]`` cut mid-object and handed the model invalid JSON with
    no signal that anything was missing — that is how 13 of 37 board items got reported as
    the whole board. Trim by ITEM COUNT and always state returned/total.
    """
    shown = [_slim_task(t) for t in items[:limit]]
    body = {
        "ok": payload.get("ok"),
        "message": payload.get("message"),
        "returned": len(shown),
        "total": len(items),
        "truncated": len(items) > len(shown),
        "tasks": shown,
    }
    if body["truncated"]:
        body["note"] = (
            f"Showing {len(shown)} of {len(items)} items. Do NOT state that something is "
            f"absent from the board based on this partial list."
        )
    return json.dumps(body, default=str)


async def _plaky_list_boards() -> str:
    """Return all boards (id + name) from Plaky — use when placement is unset or user asks what exists."""
    c = PlakyClient()
    raw = await c.list_boards()
    return json.dumps(raw, default=str)[:12000]


async def _plaky_list_tasks(status: str = "all", board_id: str = "") -> str:
    """List board items. Defaults to ALL statuses: descriptive questions ("what is on this
    board?") must see finished work too, and the old "open" default silently dropped
    Completed items, which the model then reported as "nothing is Completed"."""
    from boardman.agent.tool_context import get_context_plaky_board_id

    c = PlakyClient()
    bid = (board_id or "").strip() or (get_context_plaky_board_id() or "").strip() or None
    r = await c.get_tasks(status=status, board_id=bid)
    tasks = r.get("tasks") if isinstance(r, dict) else None
    if isinstance(tasks, list):
        payload = dict(r)
        payload["applied_status_filter"] = status
        return _envelope(payload, tasks)
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


def _summary_line(text: str, limit: int = 320) -> str:
    """One readable sentence from a task description.

    A hard slice mid-word ("...provider fall") reached the user verbatim, because the
    model is told to echo this receipt as written — it read as the assistant cutting
    out mid-thought. End on a sentence, else on a word, and mark the cut so an elision
    is obviously an elision.
    """
    first = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    if not first or len(first) <= limit:
        return first
    window = first[: limit + 1]
    # A complete first sentence is the best summary at any sensible length; the floor
    # only rejects a fragment so short it says nothing.
    best = -1
    for stop in (". ", "! ", "? "):
        best = max(best, window.rfind(stop))
    if best >= 30:
        return window[: best + 1].strip()
    space = window.rfind(" ")
    return (window[:space] if space >= limit // 2 else first[:limit]).rstrip(" ,;:-") + "…"


async def _board_group_index(board_id: str) -> dict[str, str]:
    """{group_id: group_name} for a board, or {} when the board cannot be read.

    Empty means "unknown", never "no groups" — a failed read must not be treated as
    proof that a group the caller named is invalid.
    """
    try:
        res = await PlakyClient().list_groups(board_id)
    except Exception:
        return {}
    if not res.get("ok", True):
        return {}
    out: dict[str, str] = {}
    for g in res.get("groups") or []:
        if isinstance(g, dict) and g.get("id"):
            out[str(g["id"])] = str(g.get("title") or g.get("name") or "")
    return out


def _group_for_repo_name(groups: dict[str, str], repo: str) -> str:
    """Group whose name matches a repo (Bots board names each group after its repo)."""
    short = (repo or "").strip().rsplit("/", 1)[-1].casefold()
    if not short:
        return ""
    for gid, name in groups.items():
        if name.strip().casefold() == short:
            return gid
    for gid, name in groups.items():
        n = name.strip().casefold()
        if n and (n in short or short in n):
            return gid
    return ""


def resolve_people_to_field_values(
    *,
    assignee: str,
    qa: str,
    normalized: dict[str, Any] | None,
) -> tuple[dict[str, str], list[str]]:
    """Turn typed names ("Ali", "sergiovargas111") into person-column values.

    Returns (field_values, notes). A name that cannot be resolved confidently is
    reported in notes and left unset — assigning the wrong teammate is worse than
    leaving the column empty, and the note names the near-misses so the assistant can
    ask instead of guessing.
    """
    from boardman.assignment.person_match import (
        ambiguous_candidates,
        best_member_for_name,
    )

    out: dict[str, str] = {}
    notes: list[str] = []
    wanted = [("assignee", (assignee or "").strip()), ("qa", (qa or "").strip())]
    if not any(v for _role, v in wanted):
        return out, notes

    cfg = load_team_assignments()
    people = list(cfg.members) + list(getattr(cfg, "fallback_members", []) or [])
    keys = infer_plaky_field_keys_from_normalized(normalized) if normalized else {}
    field_for = {"assignee": keys.get("engineer") or "", "qa": keys.get("qa") or ""}

    for role, name in wanted:
        if not name:
            continue
        key = field_for.get(role) or ""
        if not key:
            notes.append(f"{role} {name!r} not set: this board has no {role} person column")
            continue
        hit = best_member_for_name(name, people)
        if hit is None:
            near = ambiguous_candidates(name, people)
            hint = f" Did you mean: {', '.join(near)}?" if near else ""
            notes.append(f"{role} {name!r} did not match one person, so it was left unset.{hint}")
            continue
        member_id = str(getattr(hit.member, "id", "") or "").strip()
        if not member_id:
            notes.append(
                f"{role} {name!r} matched {hit.member.display!r} but they have no Plaky id"
            )
            continue
        out[key] = member_id
        notes.append(f"{role} -> {hit.member.display} ({hit.reason}, {hit.score:.2f})")
    return out, notes


async def _plaky_create_task(
    title: str,
    description: str,
    priority: str = "Medium",
    repo_tag: str = "",
    board_id: str = "",
    group_id: str = "",
    field_values_json: str = "",
    auto_assign_team: bool = False,
    assignee: str = "",
    qa: str = "",
) -> str:
    from boardman.agent.task_draft import load_task_draft, merge_draft_into_field_values
    from boardman.agent.tool_context import (
        get_agent_session_pk,
        get_context_plaky_board_id,
        get_context_plaky_group_id,
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

    pk = get_agent_session_pk()
    if pk is not None:
        # A FRESH session per read: the batch tool runs creates on two concurrent lanes,
        # and SQLAlchemy forbids concurrent operations on one AsyncSession — sharing the
        # context session here aborted whole batches mid-create under unlucky timing.
        from boardman.database.session import async_session as _fresh_session

        async with _fresh_session() as _draft_db:
            draft = await load_task_draft(_draft_db, pk)
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
        # Drop configured keys the board does not actually have. team_assignments.yml is
        # global but field keys are per-board (e.g. repo tag-2 exists on some boards and not
        # on 269031), and sending an unknown key makes Plaky reject the whole create with
        # "Item field doesn't exist" — which surfaced to the user as a failed task creation.
        board_keys: set[str] = set()
        if normalized:
            board_keys = {
                str(f.get("key") or "").strip()
                for f in (normalized.get("fields") or [])
                if isinstance(f, dict) and f.get("key")
            }
            if repo_k and repo_k not in board_keys:
                repo_k = ""
            if gh_k and gh_k not in board_keys:
                gh_k = ""
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
            if key in parsed:
                continue
            # The scrub above only cleared the LOCAL variables; build_repo_field_map falls
            # back to cfg.plaky_field_repo on its own, so the global key comes right back.
            # team_assignments.yml names tag-2 (an old demo board), the Bots board has no
            # such column, and one absent key refused every row of a 5-task batch while the
            # model reported failure instead of retrying. Filter against the real board.
            if board_keys and key not in board_keys:
                continue
            merged[key] = value

    person_notes: list[str] = []
    if (assignee or qa) and normalized is None and effective_board:
        bundle = await fetch_board_schema_bundle(effective_board)
        normalized = (
            bundle.get("normalized") if isinstance(bundle.get("normalized"), dict) else None
        )
    if assignee or qa:
        # Names resolve here, in-process, instead of costing the model a
        # plaky_list_workspace_users round trip per task. Explicit field_values win.
        people_fv, person_notes = resolve_people_to_field_values(
            assignee=assignee, qa=qa, normalized=normalized
        )
        for key, value in people_fv.items():
            if key not in parsed:
                merged[key] = value
            else:
                # An explicit field_values entry wins, so the resolved person was NOT
                # written. The receipt must not name them.
                person_notes = [n for n in person_notes if f"-> {value}" not in n]
                person_notes.append(
                    f"{key} was set explicitly in field_values, so the typed name was ignored"
                )

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
        # Resolve near-miss labels ("Feature" on a board whose Type options are
        # Story/Task/Bug/Research) the same way the GitHub automation does, so the
        # assistant is not rejected for a word the rest of the service accepts.
        merged, coercion_notes = coerce_field_values(merged, normalized)
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
                    "message": "; ".join(errors),
                    "errors": errors,
                    "warnings": warnings,
                    "next_step": "Fix the values and call this tool again in this same turn. Do NOT report failure or ask the user to choose - the allowed options are listed here.",
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
    if person_notes:
        # The receipt must say who actually landed on the task, including anyone the
        # matcher refused to guess at.
        out["people_resolved"] = person_notes
    return json.dumps(out, default=str)


async def _plaky_create_tasks_deferred(
    tasks_json: str,
    board_id: str = "",
    group_id: str = "",
    auto_assign_team: bool = False,
) -> str:
    """Hand a batch of tasks to the board and return before the writes finish.

    Plaky takes tens of seconds for five tasks, and none of that has to happen while the
    person waits. The rows are validated here (cheap, local), persisted as a job, and
    written behind the reply — so Boardman answers in a few seconds and the cards appear
    on the board shortly after.

    The receipt this returns describes what is BEING created, never what was created.
    """
    from boardman.agent.tool_context import get_context_plaky_board_id, get_context_plaky_group_id
    from boardman.jobs.deferred import enqueue_and_run_soon

    try:
        rows = json.loads(tasks_json or "[]")
    except ValueError as e:
        return json.dumps({"ok": False, "message": f"tasks_json is not valid JSON: {e}"})
    if not isinstance(rows, list) or not rows:
        return json.dumps({"ok": False, "message": "tasks_json must be a non-empty JSON array"})
    if len(rows) > 20:
        return json.dumps({"ok": False, "message": "at most 20 tasks per call"})

    clean: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not str(row.get("title") or "").strip():
            return json.dumps({"ok": False, "message": "every task needs a non-empty title"})
        clean.append(row)

    bid = (board_id or "").strip() or (get_context_plaky_board_id() or "").strip()
    gid = (group_id or "").strip() or (get_context_plaky_group_id() or "").strip()

    # Verify the placement is REAL before anything is written. Asked for tasks on
    # deepiri-sorge, the model supplied board 218752 / group 269028 (269028 is the
    # board, not a group); Plaky rejected all five rows with "Non-existent item group"
    # and the user was told they had been created. Ids the model invents must never
    # reach the write path.
    placement_note = ""
    if bid:
        groups = await _board_group_index(bid)
        if groups and gid not in groups:
            repo_hint = str((clean[0] or {}).get("repo_tag") or "").strip()
            fixed = _group_for_repo_name(groups, repo_hint)
            if fixed:
                placement_note = (
                    f"group {gid or '(none)'} is not on board {bid}; used "
                    f"{groups[fixed]} ({fixed}) which matches {repo_hint}"
                )
                gid = fixed
            else:
                return json.dumps(
                    {
                        "ok": False,
                        "message": (
                            f"group_id {gid or '(none)'} does not exist on board {bid}. "
                            "Do not guess placement ids. Valid groups on this board: "
                            + ", ".join(f"{k} = {v}" for k, v in groups.items())
                            + ". Call this tool again with one of those group_ids."
                        ),
                        "valid_groups": groups,
                    },
                    default=str,
                )

    # Dedupe BEFORE replying. This is one board listing (~1s) and it is the only way the
    # reply can be true: deferring it too made Boardman announce "5 new tasks" while
    # three of them were already on the board. The slow part is the WRITES, so only
    # those go behind the reply.
    existing: list[dict[str, Any]] = []
    dedupe_ok = False
    if bid:
        try:
            listing = await PlakyClient().get_tasks(board_id=bid, status="all")
            existing = [t for t in (listing.get("tasks") or []) if isinstance(t, dict)]
            dedupe_ok = bool(listing.get("ok", True))
        except Exception:
            dedupe_ok = False

    already: list[dict[str, Any]] = []
    to_create: list[dict[str, Any]] = []
    for row in clean:
        hit = None
        for ex in existing:
            ex_title = str(ex.get("name") or ex.get("title") or "")
            if _titles_match(str(row["title"]), ex_title):
                hit = ex
                break
        if hit is None:
            to_create.append(row)
        else:
            ex_id = str(hit.get("id") or "")
            already.append(
                {
                    "title": str(row["title"]).strip(),
                    "existing_title": str(hit.get("name") or hit.get("title") or ""),
                    "task_id": ex_id,
                    "task_url": f"https://app.plaky.com/task/{ex_id}" if ex_id else "",
                }
            )

    job_id = ""
    if to_create:
        job_id = await enqueue_and_run_soon(
            "plaky_create_tasks_job",
            {
                "tasks": to_create,
                "board_id": bid,
                "group_id": gid,
                "auto_assign_team": bool(auto_assign_team),
            },
        )

    cards: list[str] = []
    for i, row in enumerate(clean):
        title = str(row.get("title")).strip()
        head = f"{i + 1}.) **{title}**"
        dupe = next((a for a in already if a["title"] == title), None)
        if dupe:
            link = f" · [open in Plaky]({dupe['task_url']})" if dupe["task_url"] else ""
            cards.append(
                f"{head}\n    Already in Plaky as **{dupe['existing_title']}** — "
                f"not re-created{link}"
            )
            continue
        # Reads as finished work. The write is seconds behind the reply and the person
        # asking does not care about the queue; if one ever fails, the next turn says so
        # (see recent_failed_task_writes) rather than every reply hedging.
        if row.get("assignee"):
            bits = [f"Assignee {str(row['assignee']).strip()}"]
        else:
            bits = ["Status **NEEDS ASSIGNED**"]
        bits.append(f"Priority **{str(row.get('priority') or 'Medium').strip()}**")
        bits.append(f"QA {str(row['qa']).strip()}" if row.get("qa") else "QA assigned at PR time")
        summary = _summary_line(str(row.get("description") or ""))
        reason = f"\n    {summary}" if summary else ""
        cards.append(f"{head}\n    {' · '.join(bits)}{reason}")

    note = (
        f"{len(already)} of {len(clean)} already existed and were NOT re-created; "
        f"{len(to_create)} were set up and show on the board within a few seconds. "
        'Write it the way a colleague would: "Here\'s what I created" / "Here are the '
        'N tasks", then echo receipt_markdown. Do NOT narrate the queue - no '
        "'queuing', no 'creating now', no 'they will land shortly'. From the user's "
        "point of view the work is done. Keep the 'Already in Plaky' lines exactly as "
        "written: they must be told which ones already existed, never that all are new. "
        "Do not quote task ids you were not given. Then add a short paragraph of YOUR "
        "reasoning: why these pieces of work, in this order, and what you deliberately "
        "left out. The bare list on its own is not an acceptable answer."
    )
    if placement_note:
        note += f" Placement note: {placement_note}."
    if not dedupe_ok and bid:
        note += (
            " WARNING: the board listing failed, so duplicates could not be checked - "
            "say that some of these may already exist rather than claiming they are new."
        )
    return json.dumps(
        {
            "ok": True,
            "deferred": True,
            "queued_count": len(to_create),
            "already_existed_count": len(already),
            "already_existed": already,
            "dedupe_checked": dedupe_ok,
            "job_id": job_id,
            "board_id": bid,
            "group_id": gid,
            "receipt_markdown": "\n\n".join(cards),
            "note": note,
        },
        default=str,
    )


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
        parsed, coercion_notes = coerce_field_values(parsed, normalized)
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
                    "message": "; ".join(errors),
                    "errors": errors,
                    "warnings": warnings,
                    "next_step": "Fix the values and call this tool again in this same turn. Do NOT report failure or ask the user to choose - the allowed options are listed here.",
                },
                default=str,
            )
        parsed = cleaned
        field_validation_warnings = warnings
    c = PlakyClient()
    r = await c.patch_item_field_values(bid, item_id.strip(), parsed)
    out = dict(r) if isinstance(r, dict) else {"result": r}

    # Patch put a person in the engineer/assignee column without a status in the same
    # request: make Status agree with the Assignee (NEEDS ASSIGNED -> Assigned only).
    if out.get("ok"):
        wrote_engineer = False
        wrote_status = False
        for f in (normalized or {}).get("fields") or []:
            if not isinstance(f, dict):
                continue
            key = str(f.get("key") or "")
            if key not in parsed:
                continue
            name = str(f.get("name") or "").casefold()
            ftype = str(f.get("type") or "").upper()
            # Only the ownership column counts — patching "Reviewer"/"Reporter" person
            # columns says nothing about who is assigned.
            if "PERSON" in ftype and any(
                tok in name for tok in ("assignee", "engineer", "developer", "owner", "dev")
            ):
                wrote_engineer = True
            # Any select-like workflow column counts as an explicit status write, whatever
            # the board named it ("Status", "State", "Workflow").
            if ("STATUS" in ftype or "SELECT" in ftype) and any(
                tok in name for tok in ("status", "state", "workflow")
            ):
                wrote_status = True
        if wrote_engineer and not wrote_status:
            from boardman.services.task_mutations import bump_status_for_assignee

            bumped = await bump_status_for_assignee(bid, item_id.strip(), c)
            if bumped:
                out.update(bumped)

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


def _normalize_title(t: str) -> str:
    import re as _re

    return " ".join(_re.sub(r"[^a-z0-9 ]+", " ", (t or "").casefold()).split())


def _titles_match(a: str, b: str) -> bool:
    """Same task in different words: exact normalized match, or heavy token overlap."""
    na, nb = _normalize_title(a), _normalize_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    if len(ta) < 3 or len(tb) < 3:
        return False
    inter = len(ta & tb)
    return inter / max(1, len(ta | tb)) >= 0.75


async def _plaky_create_tasks(
    tasks_json: str,
    board_id: str = "",
    group_id: str = "",
    auto_assign_team: bool = False,
) -> str:
    """Create MANY Plaky tasks in ONE call - concurrent creates, one receipt per task.

    "Create me 5 tasks" used to cost one full LLM round trip PER task (~25-30s each at
    this prompt size against the org's TPM ceiling) because the model could only emit
    one create at a time. Batching moves the loop below the model: one round trip, and
    the creates run concurrently (bounded, so Plaky's rate limit is not stampeded).
    """
    import asyncio as _asyncio

    try:
        rows = json.loads(tasks_json or "[]")
    except ValueError as e:
        return json.dumps({"ok": False, "message": f"tasks_json is not valid JSON: {e}"})
    if not isinstance(rows, list) or not rows:
        return json.dumps({"ok": False, "message": "tasks_json must be a non-empty JSON array"})
    if len(rows) > 20:
        return json.dumps(
            {
                "ok": False,
                "message": (
                    "at most 20 tasks per call - split the list and call plaky_create_tasks "
                    "again with the remainder (successive calls are fine; do not fall back "
                    "to single creates)"
                ),
            }
        )

    # Duplicate guard: the board is the source of truth. Creating "Ship bidirectional
    # sync" when that card already exists buries the real one - fetch existing titles
    # ONCE and skip matches, pointing at the existing card instead.
    from boardman.agent.tool_context import get_context_plaky_board_id

    existing: list[dict[str, Any]] = []
    dedupe_bid = (board_id or "").strip() or (get_context_plaky_board_id() or "").strip()
    if dedupe_bid:
        try:
            listing = await PlakyClient().get_tasks(board_id=dedupe_bid, status="all")
            existing = [t for t in (listing.get("tasks") or []) if isinstance(t, dict)]
        except Exception:
            existing = []  # dedupe is best-effort; creation must not die on a listing blip

    # Plaky's create endpoint is ~0.2s solo but shapes concurrent bursts hard (measured
    # 2.5-11.8s per POST at 5-way). Two lanes with a 0.3s stagger measured fastest
    # (12.9s for 5 vs 17.8s at 5-way). QA is NOT picked here (auto_assign_team defaults
    # False) - employer flow assigns QA at PR time.
    sem = _asyncio.Semaphore(2)

    async def one(row: Any, idx: int = 0) -> dict[str, Any]:
        if not isinstance(row, dict) or not str(row.get("title") or "").strip():
            return {
                "ok": False,
                "title": str((row or {}).get("title") or ""),
                "message": "title is required",
            }
        for ex in existing:
            ex_title = str(ex.get("name") or ex.get("title") or "")
            if _titles_match(str(row["title"]), ex_title):
                ex_id = str(ex.get("id") or "")
                return {
                    "ok": True,
                    "already_exists": True,
                    "title": row["title"],
                    "existing_title": ex_title,
                    "task_id": ex_id,
                    "task_url": f"https://app.plaky.com/task/{ex_id}" if ex_id else "",
                    "message": "",
                }
        fv = row.get("field_values")
        # Capped lane stagger AFTER validation: spreads burst arrival without making a
        # 20-row batch's tail idle for seconds, and invalid rows fail instantly.
        await _asyncio.sleep(min(idx, 4) * 0.25)
        async with sem:
            raw = await _plaky_create_task(
                title=str(row["title"]),
                description=str(row.get("description") or ""),
                priority=str(row.get("priority") or "Medium"),
                repo_tag=str(row.get("repo_tag") or ""),
                board_id=board_id,
                group_id=group_id,
                field_values_json=json.dumps(fv) if isinstance(fv, dict) and fv else "",
                auto_assign_team=auto_assign_team,
                assignee=str(row.get("assignee") or ""),
                qa=str(row.get("qa") or ""),
            )
        try:
            res = json.loads(raw)
        except ValueError:
            return {"ok": False, "title": row["title"], "message": str(raw)[:300]}
        task = res.get("task") if isinstance(res.get("task"), dict) else {}
        fv_keys = {str(k) for k in fv} if isinstance(fv, dict) else set()
        return {
            "ok": bool(res.get("ok")),
            "title": row["title"],
            "task_id": str(task.get("id") or res.get("task_id") or ""),
            "task_url": res.get("task_url") or "",
            "priority": str(row.get("priority") or "Medium"),
            # Only claim the default status when the row did not set its own status or
            # people - otherwise the receipt could contradict the board. assignee/qa
            # count: they resolve into person columns, so the status follows the
            # assignee and the card must not print "NEEDS ASSIGNED / QA at PR time".
            "default_status_applies": (
                not auto_assign_team
                and not row.get("assignee")
                and not row.get("qa")
                and not any(k.startswith(("status", "person")) for k in fv_keys)
            ),
            # A name the matcher refused is the one thing the user must hear about.
            "people_resolved": res.get("people_resolved") or [],
            "message": "" if res.get("ok") else str(res.get("message") or "")[:300],
        }

    raw_results = await _asyncio.gather(
        *(one(r, i) for i, r in enumerate(rows)), return_exceptions=True
    )
    results = [
        (
            r
            if isinstance(r, dict)
            else {
                "ok": False,
                "title": str(
                    (rows[i] or {}).get("title") if isinstance(rows[i], dict) else rows[i]
                )[:80],
                "message": f"{type(r).__name__}: {r}"[:300],
            }
        )
        for i, r in enumerate(raw_results)
    ]
    created = [r for r in results if r.get("ok") and not r.get("already_exists")]
    skipped_existing = [r for r in results if r.get("already_exists")]
    failed = [r for r in results if not r.get("ok")]
    # Numbered receipt cards, one per input row — built from results ALONE so a
    # malformed row still gets a visible failure card instead of vanishing from the
    # receipt while counting in failed_count.
    cards: list[str] = []
    for i, r in enumerate(results, start=1):
        head = f"{i}.) **{r.get('title')}**"
        link = f" · [open in Plaky]({r['task_url']})" if r.get("task_url") else ""
        if r.get("already_exists"):
            cards.append(
                f"{head}\n    Already on the board as **{r.get('existing_title')}** — "
                f"not re-created{link}"
            )
        elif r.get("ok"):
            bits = []
            if r.get("default_status_applies", True):
                bits.append("Status **NEEDS ASSIGNED**")
            bits.append(f"Priority **{r.get('priority') or 'Medium'}**")
            cards.append(f"{head}\n    {' · '.join(bits)} · QA assigned at PR time{link}")
        else:
            cards.append(f"{head}\n    ⚠ FAILED: {r.get('message')}")
    return json.dumps(
        {
            "ok": not failed,
            "created_count": len(created),
            "already_existed_count": len(skipped_existing),
            "failed_count": len(failed),
            "results": results,
            "receipt_markdown": "\n\n".join(cards),
            "note": "Echo receipt_markdown to the user (adjust only fields you know differ), add ONE closing line. Do not re-compose the receipts from scratch.",
        },
        default=str,
    )


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
                    coroutine=_plaky_create_tasks,
                    name="plaky_create_tasks",
                    description=(
                        "Create SEVERAL Plaky tasks in ONE call - the ONLY correct way to create 2+ "
                        "tasks. Never loop plaky_create_task for a multi-task request. "
                        "Args: tasks_json = JSON array of {title (required), description?, priority?, "
                        "repo_tag?, assignee? (PLAIN NAME e.g. 'Ali' or a GitHub login - resolved "
                        "server-side, never pass an id), qa? (plain name, same rule; set it whenever "
                        "the user asks for QA on these tasks - that is an explicit request, not "
                        "auto-assignment), "
                        "field_values? (object, schema keys)}; board_id?/group_id? apply to "
                        "all (Current Plaky placement used when omitted); auto_assign_team defaults "
                        "FALSE - QA is assigned at PR time per team flow, not at creation. "
                        "Creates run concurrently server-side. Returns one receipt per task "
                        "(ok, task_id, task_url, message)."
                    ),
                ),
                StructuredTool.from_function(
                    coroutine=_plaky_create_tasks_deferred,
                    name="plaky_create_tasks_deferred",
                    description=(
                        "PREFERRED for creating 2+ Plaky tasks. Same rows as "
                        "plaky_create_tasks, but it returns as soon as the work is handed "
                        "off instead of waiting out the board writes, so you can answer in "
                        "seconds while the cards land shortly after. "
                        "Args: tasks_json = JSON array of {title (required), description?, "
                        "priority?, repo_tag?, assignee? (plain name), qa? (plain name), "
                        "field_values?}; board_id?/group_id? apply to all. "
                        "It checks the board for duplicates BEFORE returning, so the receipt "
                        "already knows which rows are new and which are 'Already in Plaky'. "
                        "Echo receipt_markdown as-is, keep the 'Already in Plaky' lines, and "
                        "never tell the user all of them are new. Report the rest as done "
                        '("here is what I created") - they appear on the board within '
                        "seconds, so do not narrate the queue. Do not quote task ids you "
                        "were not given. Then add YOUR reasoning: why this work, in this "
                        "order, what you left out. Use plaky_create_tasks instead ONLY when "
                        "the user needs the task ids in this same reply."
                    ),
                ),
                StructuredTool.from_function(
                    coroutine=_plaky_create_task,
                    name="plaky_create_task",
                    description=(
                        "Create a Plaky item. Call plaky_board_schema first if field_values_json is non-empty. "
                        "field_values_json keys MUST match schema key= strings; assignee ids from plaky_list_workspace_users. "
                        "Placement: pass board_id/group_id or rely on Current Plaky placement. "
                        "Args: title, description, priority (High|Low|Medium|Very Important or legacy low|medium|high), "
                        "repo_tag?, board_id?, group_id?, field_values_json?, assignee?, qa?, "
                        "auto_assign_team (default FALSE - QA is picked when a PR opens). "
                        "assignee/qa take a PLAIN NAME ('Ali', 'sergio', 'AndyN-star'); Boardman fuzzy-matches "
                        "it to the roster in-process, so do NOT call plaky_list_workspace_users first and do "
                        "NOT pass numeric ids. An unresolvable or ambiguous name is reported back in "
                        "people_resolved and the column is left empty - relay that, never invent a person. "
                        "ALWAYS pass assignee/qa when the user names a person ('assign it to Ali', 'Sergio "
                        "should QA this'). An explicit request is not auto-assignment: auto_assign_team=False "
                        "only stops Boardman from PICKING a QA on its own, it never blocks a QA the user asked "
                        "for. Refusing a named assignee because 'QA happens at PR time' is wrong. "
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
