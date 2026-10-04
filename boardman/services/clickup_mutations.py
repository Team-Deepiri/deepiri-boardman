"""ClickUp counterpart of ``update_task_internal``.

Same input (``UpdateTaskInput``) and the same ``{"ok", "task_id", "operations"}`` result, so the
``PATCH /tasks/{id}`` route, the CLI and the agent can call one entry point whichever provider is
active. QA is picked by the shared ``pick_qa_for_repo`` (provider-neutral: it ranks the GitHub
roster) and applied with ``ClickUpClient.assign_qa``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from boardman.assignment.config import load_team_assignments
from boardman.assignment.qa_picker import ensure_github_owner_repo, pick_qa_for_repo
from boardman.clickup.client import ClickUpClient

if TYPE_CHECKING:
    from boardman.services.task_mutations import (
        CreateSubtaskInput,
        CreateTaskInput,
        UpdateTaskInput,
    )


def _engineer_refusal(req: UpdateTaskInput) -> dict[str, Any] | None:
    """Engineer assignment is not supported yet (it needs the eligibility rules from the webhook
    sync). Report it; the caller still applies the other fields."""
    if (req.engineer_plaky_id or "").strip() or req.clear_engineer_assignee:
        return {
            "ok": False,
            "message": "Engineer assignment is not supported on ClickUp yet; the other fields were still applied.",
        }
    return None


async def _auto_pick_qa(req: UpdateTaskInput) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """Pick QA for ``req.github_repo``. Returns ``(qa_id, operation_record, error_result)``."""
    repo_in = (req.github_repo or "").strip()
    if not repo_in:
        return (
            "",
            {},
            {
                "ok": False,
                "status": 400,
                "message": "github_repo is required when auto_assign_qa is enabled and qa_plaky_id is not provided",
            },
        )
    repo = ensure_github_owner_repo(repo_in)
    picked, reason = await pick_qa_for_repo(repo, load_team_assignments())
    record = {
        "ok": bool((picked or "").strip()),
        "repo": repo,
        "picked_qa_user_id": picked,
        "reason": reason,
    }
    if not picked:
        error = {
            "ok": False,
            "status": 400,
            "message": f"Could not auto-assign QA for repo '{repo}': {reason}",
        }
        return "", record, error
    return str(picked).strip(), record, None


def _overall(ops: dict[str, Any]) -> bool:
    verdicts = [v for v in ops.values() if "ok" in v and not v.get("skipped")]
    return all(bool(v["ok"]) for v in verdicts)


async def update_clickup_task(
    task_id: str,
    req: UpdateTaskInput,
    *,
    add_assignee_ids: list[int] | None = None,
    client: ClickUpClient | None = None,
) -> dict[str, Any]:
    """Apply an ``UpdateTaskInput`` to a ClickUp task.

    Supported: status, priority, title, description, QA (explicit id or auto-assigned from
    ``github_repo``), and extra assignees. ``task_type`` has no ClickUp equivalent and is
    reported as skipped. Developer (engineer) assignment is reported as not applied (it needs the
    eligibility rules that arrive with the webhook sync) while the other fields still go through.

    The top-level ``ok`` is all-or-nothing: it is False if any requested part failed or was not
    applied, even when others succeeded. Read ``operations`` for the per-part outcome, for example
    ``operations.task_fields.ok`` is True while ``operations.engineer.ok`` is False.
    """
    c = client or ClickUpClient()
    ops: dict[str, Any] = {}

    engineer = _engineer_refusal(req)
    if engineer:
        ops["engineer"] = engineer

    qa_id = (req.qa_plaky_id or "").strip()
    if req.auto_assign_qa and not qa_id:
        qa_id, record, error = await _auto_pick_qa(req)
        if record:
            ops["qa_auto_assign"] = record
        if error:
            return {**error, "operations": ops} if ops else error

    status = (req.status or "").strip()
    priority = (req.priority or "").strip()
    wants_fields = any(
        [status, priority, req.title is not None, req.description is not None, add_assignee_ids]
    )
    if not (wants_fields or qa_id):
        if engineer:
            return {"ok": False, "status": 400, "message": engineer["message"], "operations": ops}
        return {"ok": False, "status": 400, "message": "No update fields provided"}

    if (req.task_type or "").strip():
        ops["task_type"] = {
            "ok": True,
            "skipped": True,
            "message": "ClickUp has no task type field; ignored.",
        }
    if wants_fields:
        res = await c.update_task_fields(
            task_id,
            title=req.title,
            description=req.description,
            priority=priority or None,
            status=status or None,
            add_assignee_ids=add_assignee_ids,
        )
        res.pop("task", None)
        ops["task_fields"] = res
    if qa_id:
        ops["qa"] = await c.assign_qa(task_id, qa_id)

    return {"ok": _overall(ops), "task_id": task_id, "operations": ops}


# -- create ----------------------------------------------------------------------------------------


def _create_status(explicit: str, has_owner: bool) -> str:
    """A status the caller named is used as written (ClickUp statuses are per list). Otherwise it
    follows ownership, like an issue: an owner means "assigned", nobody means "needs assigned"."""
    from boardman.clickup.statuses import status_for_intent

    if explicit.strip():
        return explicit.strip()
    return status_for_intent("workflow_assigned" if has_owner else "workflow_needs_assigned")


async def _qa_for_create(qa_id: str, auto: bool, repo_full: str) -> tuple[str, str]:
    """(QA id to assign, why). Explicit wins; otherwise pick for the repo when asked to."""
    if qa_id:
        return qa_id, "explicit"
    if auto and repo_full:
        picked, reason = await pick_qa_for_repo(repo_full, load_team_assignments())
        return (str(picked).strip() if picked else ""), reason
    return "", "QA is picked when a PR opens"


async def create_clickup_task(
    req: CreateTaskInput, *, client: ClickUpClient | None = None
) -> dict[str, Any]:
    """ClickUp counterpart of ``create_task_internal``: the same input, one ClickUp call.

    The list is ``plaky_board_id`` (a ClickUp list id), else the request's placement context, else
    ``CLICKUP_DEFAULT_LIST_ID``. The status follows ownership unless one was named. A QA is assigned
    only when one is named or ``auto_assign_team`` is on and a repo is known. Repo names and the
    type become tags. ``field_values`` and ``plaky_group_id`` have no ClickUp meaning and are ignored.
    """
    from boardman.agent.tool_context import get_context_placement_id
    from boardman.assignment.developer_eligibility import filter_developer
    from boardman.plaky.task_tag_vocab import (
        canonical_task_priority,
        canonical_task_type,
    )
    from boardman.services.task_mutations import _merge_github_repo_inputs
    from boardman.settings import settings

    c = client or ClickUpClient()
    f = req.filters if isinstance(req.filters, dict) else {}
    title = (req.title or "").strip() or str(f.get("title") or "").strip()
    if not title:
        return {"ok": False, "status": 400, "message": "title is required"}
    description = (req.description or "").strip() or str(f.get("description") or "").strip()
    raw_status = (req.status or "").strip() or str(f.get("status") or "").strip()
    canon_type = canonical_task_type(
        (req.task_type or "").strip() or str(f.get("type") or f.get("task_type") or "").strip()
    )
    canon_priority = canonical_task_priority(
        (req.priority or "").strip() or str(f.get("priority") or "").strip()
    )
    engineer_in = (req.engineer_plaky_id or "").strip() or str(
        f.get("engineer_plaky_id") or ""
    ).strip()
    qa_in = (req.qa_plaky_id or "").strip() or str(f.get("qa_plaky_id") or "").strip()
    repos = _merge_github_repo_inputs(
        primary_repo=str(f.get("repo") or "").strip(), extra_repos=req.github_repos, filters=f
    )
    repo_full = repos[0] if repos else (req.repo or "").strip()

    list_id = (
        (req.plaky_board_id or "").strip()
        or (get_context_placement_id() or "").strip()
        or (settings.clickup_default_list_id or "").strip()
    )
    if not list_id:
        return {
            "ok": False,
            "status": 400,
            "message": "No ClickUp list. Pass a list id or set CLICKUP_DEFAULT_LIST_ID.",
        }

    developer, refusal = filter_developer(engineer_in)
    if repos:
        description = f"{description}\n\nRepo: {', '.join(repos)}".strip()
    tags = [r.rsplit("/", 1)[-1].lower() for r in repos]
    if canon_type:
        tags.append(f"type:{canon_type.strip().lower()}")
    result = await c.create_task(
        title,
        description,
        canon_priority,
        board_id=list_id,
        status=_create_status(raw_status, bool(developer)) or None,
        assignee_ids=[int(developer)] if developer.isdigit() else None,
        tags=list(dict.fromkeys(tags)) or None,
    )
    if not result.get("ok"):
        return result
    task_id = str(result.get("task_id") or "")
    qa_id, why = await _qa_for_create(qa_in, bool(req.auto_assign_team), repo_full)
    if qa_id and task_id:
        result["qa"] = {**await c.assign_qa(task_id, qa_id), "reason": why[:220]}
    if refusal:
        result["developer_not_assigned"] = refusal
    return result


async def create_clickup_subtask(
    req: CreateSubtaskInput, *, client: ClickUpClient | None = None
) -> dict[str, Any]:
    """ClickUp counterpart of ``create_subtask_internal``. The subtask lands in the parent's list."""
    from boardman.agent.tool_context import get_context_placement_id
    from boardman.assignment.developer_eligibility import filter_developer
    from boardman.plaky.task_tag_vocab import canonical_task_priority
    from boardman.services.task_mutations import _merge_github_repo_inputs

    c = client or ClickUpClient()
    parent = (req.parent_task_id or "").strip()
    title = (req.title or "").strip()
    if not parent:
        return {"ok": False, "status": 400, "message": "parent_task_id is required"}
    if not title:
        return {"ok": False, "status": 400, "message": "title is required"}
    repos = _merge_github_repo_inputs(primary_repo="", extra_repos=req.github_repos, filters={})
    developer, refusal = filter_developer((req.engineer_plaky_id or "").strip())
    description = (req.description or "").strip()
    if repos:
        description = f"{description}\n\nRepo: {', '.join(repos)}".strip()
    result = await c.create_subtask(
        parent,
        title,
        description,
        status=_create_status((req.status or "").strip(), bool(developer)) or None,
        priority=canonical_task_priority((req.priority or "").strip()),
        board_id=(req.plaky_board_id or "").strip() or (get_context_placement_id() or "").strip(),
    )
    if not result.get("ok"):
        return result
    result.setdefault("parent_task_id", parent)
    task_id = str(result.get("task_id") or "")
    if developer.isdigit() and task_id:
        result["assignee"] = await c.update_task_fields(task_id, add_assignee_ids=[int(developer)])
        result["assignee"].pop("task", None)
    qa_id, why = await _qa_for_create(
        (req.qa_plaky_id or "").strip(), bool(req.auto_assign_qa), repos[0] if repos else ""
    )
    if qa_id and task_id:
        result["qa"] = {**await c.assign_qa(task_id, qa_id), "reason": why[:220]}
    if refusal:
        result["developer_not_assigned"] = refusal
    return result
