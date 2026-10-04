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
    from boardman.services.task_mutations import UpdateTaskInput


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
