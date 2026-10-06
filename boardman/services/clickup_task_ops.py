"""Small ClickUp task operations shared by the PR and review sync: read, workflow-step guards, QA lookup."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.clickup.client import ClickUpClient
from boardman.clickup.statuses import intent_for_status, status_for_intent
from boardman.clickup.task_view import current_status
from boardman.database.models import PullRequestTaskLink, SyncLog
from boardman.services.sync_state import status_intent_would_regress, status_would_move_backwards
from boardman.settings import settings


async def read_task(c: ClickUpClient, task_id: str) -> dict[str, Any] | None:
    got = await c.get_task(task_id)
    return got["task"] if got.get("ok") and isinstance(got.get("task"), dict) else None


def intent_of(task: dict[str, Any] | None) -> str:
    return intent_for_status(current_status(task)) if task else ""


async def set_intent_status(
    c: ClickUpClient,
    task_id: str,
    intent: str,
    *,
    guard: str | None = None,
    protect: tuple[str, ...] = (),
    task: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the status for ``intent``, unless a guard says not to.

    ``guard`` is "regress" (an ownership-derived write must not move work that has started
    backwards) or "backwards" (a re-run of an earlier step must not move the task backwards at
    all). ``protect`` lists intents that must never be overwritten. A guarded write on a task that
    cannot be read is skipped: one missed move is recoverable, a rewound QA queue is not.
    """
    name = status_for_intent(intent)
    if not name:
        return {"ok": True, "skipped": f"no ClickUp status configured for {intent}"}
    if task is None and (guard or protect):
        task = await read_task(c, task_id)
        if task is None:
            return {"ok": True, "skipped": "task unreadable; not moving it"}
    now = intent_of(task)
    if now and now in protect:
        return {"ok": True, "skipped": f"task is at {now}", "held_back": now}
    if guard == "regress" and status_intent_would_regress(now, intent):
        return {"ok": True, "skipped": f"task is at {now}", "held_back": now}
    if guard == "backwards" and status_would_move_backwards(now, intent):
        return {"ok": True, "skipped": f"task is at {now}", "held_back": now}
    if task is not None and current_status(task).casefold() == name.casefold():
        return {"ok": True, "skipped": "already there", "status": name}
    res = await c.update_task_fields(task_id, status=name)
    res.pop("task", None)
    return {**res, "status": name}


def stamp(session: AsyncSession, action: str, repo: str, pr: int, task_id: str, **detail: Any):
    session.add(
        SyncLog(
            action=action,
            github_repo=repo,
            github_ref=str(pr),
            plaky_task_id=task_id,
            detail=json.dumps(detail, default=str),
        )
    )


async def stamped_qa(session: AsyncSession, repo_name: str, pr_number: int, task_id: str) -> str:
    """The QA already recorded on this PR's link row for the task, or ""."""
    row = (
        await session.execute(
            select(PullRequestTaskLink.qa_plaky_id).where(
                PullRequestTaskLink.github_repo == repo_name,
                PullRequestTaskLink.github_pr_number == pr_number,
                PullRequestTaskLink.plaky_task_id == task_id,
                PullRequestTaskLink.qa_plaky_id.is_not(None),
                PullRequestTaskLink.qa_plaky_id != "",
            )
        )
    ).first()
    return str(row[0]) if row else ""


def qa_from_field(task: dict[str, Any]) -> str:
    """The QA user id held in the configured users custom field, or ""."""
    field_id = (settings.clickup_qa_field_id or "").strip()
    if not field_id:
        return ""
    for f in task.get("custom_fields") or []:
        if isinstance(f, dict) and str(f.get("id")) == field_id:
            value = f.get("value")
            if isinstance(value, list) and value:
                first = value[0]
                return str(first.get("id") if isinstance(first, dict) else first)
    return ""
