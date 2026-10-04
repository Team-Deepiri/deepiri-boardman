"""GitHub issue webhooks to ClickUp tasks. The ClickUp counterpart of the Plaky paths in
``issue_handler``; ``issue_handler`` dispatches here when ``TASK_PROVIDER=clickup``.

It keeps the same rules the Plaky path enforces, translated to ClickUp:

* No QA at task creation (QA is picked when a PR opens).
* A fresh task's status follows ownership: an owner means "assigned", nobody means "needs
  assigned". An owner has to resolve to a real, developer-eligible ClickUp member.
* Only events that carry ownership (``assigned`` / ``unassigned``) may move the status, and never
  backwards past work that has already started. Closing is always allowed.
* Priority follows GitHub only when a human set it there.
* The engineer is fill-only on events that merely carry the issue's assignee.
* Closing remembers the status the task held, so a reopen resumes it.

ClickUp can rename a task, so an edit rewrites the title and description in place (Plaky cannot,
and mirrors the edit as a comment).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.clickup.client import ClickUpClient
from boardman.clickup.statuses import intent_for_status, status_for_intent
from boardman.database.models import IssueTaskMap, SyncLog
from boardman.github.webhooks import IssueEventPayload
from boardman.repos_config import get_routing_async
from boardman.services.comment_dedupe import mirror_github_activity
from boardman.services.sync_state import (
    UNREADABLE_STATUS,
    issue_status_intent,
    resolve_issue_state,
    status_intent_would_regress,
)
from boardman.settings import settings

_log = logging.getLogger(__name__)

TYPE_TAG_PREFIX = "type:"


# -- helpers ---------------------------------------------------------------------------------


def _list_id(routing: Any | None) -> str:
    return (getattr(routing, "clickup_list_id", "") or "").strip() or (
        settings.clickup_default_list_id or ""
    ).strip()


def _task_text(state: Any, routing: Any | None) -> tuple[str, str]:
    """Title and description for an issue task."""
    title = f"[{state.repo_name}] {state.title}"
    footer = f"\n\n---\nGitHub: {state.repo_full_name}\nIssue: #{state.number}\n"
    category = (getattr(routing, "category", "") or "").strip() if routing else ""
    if category:
        footer += f"Category: {category}\n"
    return title, f"{state.body}\n\n{state.url}{footer}"


def _tags(state: Any) -> list[str]:
    tags = [state.repo_name.lower()]
    if state.task_type:
        tags.append(f"{TYPE_TAG_PREFIX}{state.task_type.strip().lower()}")
    return tags


async def _resolve_engineer(login: str) -> str:
    """The ClickUp user id that will actually be written as the developer, or "".

    Eligibility runs here so the status we derive agrees with the person we write: a task never
    reads "assigned" with nobody on it.
    """
    if not login:
        return ""
    from boardman.assignment.developer_eligibility import filter_developer
    from boardman.plaky.dynamic_qa_status import (
        github_actor_payload,
        resolve_github_user_to_plaky_user_id,
    )

    resolved = str(
        await resolve_github_user_to_plaky_user_id(github_actor_payload({"login": login})) or ""
    ).strip()
    kept, _reason = filter_developer(resolved)
    return kept


def _user_ids(user_id: str) -> list[int] | None:
    return [int(user_id)] if str(user_id).strip().isdigit() else None


def _current_status(task: dict[str, Any]) -> str:
    status = task.get("status")
    return str(status.get("status") if isinstance(status, dict) else status or "").strip()


def _current_assignees(task: dict[str, Any]) -> list[str]:
    return [str(a.get("id")) for a in task.get("assignees") or [] if isinstance(a, dict)]


def _current_type_tags(task: dict[str, Any]) -> list[str]:
    names = [str(t.get("name") or "") for t in task.get("tags") or [] if isinstance(t, dict)]
    return [n for n in names if n.startswith(TYPE_TAG_PREFIX)]


def _mapped_id(mapping: IssueTaskMap | None) -> str:
    return str((mapping.plaky_task_id if mapping else "") or "").strip()


async def _find_mapping(
    repo_name: str, issue_number: int, session: AsyncSession
) -> IssueTaskMap | None:
    return (
        await session.execute(
            select(IssueTaskMap).where(
                IssueTaskMap.github_repo == repo_name,
                IssueTaskMap.github_issue_number == issue_number,
            )
        )
    ).scalar_one_or_none()


# -- opened ----------------------------------------------------------------------------------


async def handle_issue_opened(
    payload: IssueEventPayload, session: AsyncSession, *, client: ClickUpClient | None = None
) -> dict[str, Any]:
    c = client or ClickUpClient()
    repo_name = payload.repository.name
    number = payload.issue.number

    existing = await _find_mapping(repo_name, number, session)
    if existing:
        mapped = _mapped_id(existing)
        if mapped and not mapped.startswith("pending:"):
            repaired = await handle_issue_changed(
                payload, session, event_label="issue_opened_reconciled", client=c
            )
            return {
                "ok": bool(repaired.get("ok")),
                "skipped": True,
                "message": "Issue already mapped; metadata reconciled",
                "reconciled": repaired,
            }
        return {"ok": True, "skipped": True, "message": "Issue creation is already in progress"}

    # Reserve the GitHub identity before the first POST so two deliveries for the same issue
    # cannot both create a task. The unique index is the final guard; the savepoint keeps a race
    # from rolling back the surrounding webhook transaction.
    reservation = IssueTaskMap(
        github_repo=repo_name,
        github_issue_number=number,
        plaky_task_id=f"pending:{uuid.uuid4().hex}",
    )
    try:
        async with session.begin_nested():
            session.add(reservation)
            await session.flush()
    except IntegrityError:
        if await _find_mapping(repo_name, number, session):
            return {"ok": True, "skipped": True, "message": "Issue already mapped"}
        raise

    full_name = payload.repository.full_name
    state = resolve_issue_state(payload.issue, repo_full_name=full_name, repo_name=repo_name)
    routing = await get_routing_async(full_name, repo_name, settings.github_org)
    list_id = _list_id(routing)
    if not list_id:
        await session.delete(reservation)
        return {
            "ok": False,
            "status": 400,
            "message": "No ClickUp list for this repo. Set clickup_list_id in repos.yml or CLICKUP_DEFAULT_LIST_ID.",
        }

    title, description = _task_text(state, routing)
    engineer_id = await _resolve_engineer(state.assignee_login)
    intent = issue_status_intent(state, engineer_plaky_id=engineer_id)
    status = status_for_intent(intent)

    result = await c.create_task(
        title,
        description,
        state.priority.lower(),
        board_id=list_id,
        status=status or None,
        assignee_ids=_user_ids(engineer_id),
        tags=_tags(state),
    )
    if not result.get("ok"):
        await session.delete(reservation)
        return result
    task_id = str(result.get("task_id") or "").strip()
    if not task_id:
        await session.delete(reservation)
        return {"ok": False, "message": "ClickUp task create returned no task id"}

    reservation.plaky_task_id = task_id
    reservation.plaky_task_url = result.get("task_url")
    session.add(
        SyncLog(
            action="issue_created",
            github_repo=repo_name,
            github_ref=str(number),
            plaky_task_id=task_id,
            detail=json.dumps(
                {
                    "title": title,
                    "issue_url": payload.issue.html_url,
                    "priority": state.priority,
                    "status": status,
                    "list_id": list_id,
                    # Creation sets everything in one call, so there is no separate patch that
                    # can fail. Kept so the reconcile path reads the same field as on Plaky.
                    "post_create_update_ok": True,
                },
                default=str,
            ),
        )
    )
    await session.commit()
    return {
        "ok": True,
        "plaky_task_id": task_id,
        "plaky_task_url": result.get("task_url"),
        "post_create_update": {"ok": True, "skipped": True},
    }


# -- changed (edited / assigned / unassigned / labeled) ---------------------------------------


async def _post_create_patch_failed(session: AsyncSession, task_id: str) -> bool:
    from boardman.services.issue_handler import _post_create_patch_failed as failed

    return await failed(session, task_id)


async def handle_issue_changed(
    payload: IssueEventPayload,
    session: AsyncSession,
    *,
    event_label: str = "issue_changed",
    client: ClickUpClient | None = None,
) -> dict[str, Any]:
    """Re-resolve every GitHub-owned field of the task after an edit, assignment or label event."""
    c = client or ClickUpClient()
    state = resolve_issue_state(
        payload.issue,
        repo_full_name=payload.repository.full_name,
        repo_name=payload.repository.name,
    )
    mapping = await _find_mapping(state.repo_name, state.number, session)
    task_id = _mapped_id(mapping)
    if not task_id or task_id.startswith("pending:"):
        return {"ok": True, "skipped": True, "message": "no ClickUp task mapped for this issue"}

    routing = await get_routing_async(state.repo_full_name, state.repo_name, settings.github_org)
    sync_text = payload.action == "edited" or event_label == "issue_opened_reconciled"
    title, description = _task_text(state, routing)
    engineer_id = await _resolve_engineer(state.assignee_login)
    owns_assignment = payload.action in ("assigned", "unassigned")

    got = await c.get_task(task_id)
    task = got.get("task") if got.get("ok") and isinstance(got.get("task"), dict) else None
    readable = task is not None
    task = task or {}

    repairing = event_label == "issue_opened_reconciled" and await _post_create_patch_failed(
        session, task_id
    )

    # Fill-only on events that merely carry the issue's assignee (a label edit includes the whole
    # issue): never undo a lead's manual reassignment. Only an ownership event may replace owners.
    push_engineer = engineer_id
    if push_engineer and not owns_assignment and (not readable or _current_assignees(task)):
        push_engineer = ""

    status_value = ""
    status_held_back = ""
    if state.state == "closed" or owns_assignment or repairing:
        intent = issue_status_intent(state, engineer_plaky_id=engineer_id)
        status_value = status_for_intent(intent)
        if status_value and owns_assignment and not repairing and state.state != "closed":
            now_at = intent_for_status(_current_status(task)) if readable else UNREADABLE_STATUS
            if status_intent_would_regress(now_at, intent):
                status_held_back = _current_status(task) or now_at
                status_value = ""
                if now_at == UNREADABLE_STATUS:
                    # Apply the whole ownership event or none of it: a developer written without
                    # the matching status is how a card ends up inconsistent.
                    engineer_id = push_engineer = ""
                    owns_assignment = False
    if not status_value and state.state == "closed":
        status_value = (settings.clickup_status_completed or "").strip()

    remove_ids: list[int] = []
    if payload.action == "unassigned" and payload.assignee:
        removed = await _resolve_engineer(str((payload.assignee or {}).get("login") or ""))
        remove_ids = _user_ids(removed) or []
    add_ids = _user_ids(push_engineer)

    update_kwargs: dict[str, Any] = {}
    if sync_text and title != task.get("name"):
        update_kwargs["title"] = title
    if sync_text and description != (task.get("description") or ""):
        update_kwargs["description"] = description
    if (state.priority_explicit or repairing) and readable:
        new_prio = ClickUpClient.priority(state.priority)
        current_prio = (task.get("priority") or {}).get("id") if task.get("priority") else None
        if new_prio and str(current_prio) != str(new_prio):
            update_kwargs["priority"] = state.priority
    elif (state.priority_explicit or repairing) and not readable:
        update_kwargs["priority"] = state.priority
    if status_value and status_value.casefold() != _current_status(task).casefold():
        update_kwargs["status"] = status_value
    if add_ids and not set(map(str, add_ids)) <= set(_current_assignees(task)):
        update_kwargs["add_assignee_ids"] = add_ids
    if remove_ids and set(map(str, remove_ids)) & set(_current_assignees(task)):
        update_kwargs["remove_assignee_ids"] = remove_ids

    mutation: dict[str, Any] = {"ok": True, "skipped": True}
    if update_kwargs:
        mutation = await c.update_task_fields(task_id, **update_kwargs)
        mutation.pop("task", None)

    # Type follows the issue's labels / native type: keep exactly one type tag on the task.
    type_ops: list[dict[str, Any]] = []
    wanted_tag = f"{TYPE_TAG_PREFIX}{state.task_type.strip().lower()}" if state.task_type else ""
    if (
        wanted_tag
        and payload.action in ("edited", "labeled", "unlabeled", "typed", "untyped")
        or (wanted_tag and event_label == "issue_opened_reconciled")
    ):
        have = _current_type_tags(task)
        if wanted_tag not in have:
            type_ops.append(await c.add_tag(task_id, wanted_tag))
        for stale in have:
            if stale != wanted_tag:
                type_ops.append(await c.remove_tag(task_id, stale))

    ok = bool(mutation.get("ok")) and all(bool(op.get("ok")) for op in type_ops)
    session.add(
        SyncLog(
            action=event_label,
            github_repo=state.repo_name,
            github_ref=str(state.number),
            plaky_task_id=task_id,
            detail=json.dumps(
                {
                    "event": payload.action,
                    "title": state.title,
                    "task_type": state.task_type,
                    "priority": state.priority,
                    "assignee_login": state.assignee_login,
                    "status": status_value,
                    "status_held_back": status_held_back or None,
                    "clickup_ok": mutation.get("ok"),
                    "task_readable": readable,
                },
                default=str,
            ),
        )
    )
    await session.commit()
    _log.info(
        "clickup issue sync event=%s repo=%s issue=%s task=%s status=%s ok=%s",
        payload.action,
        state.repo_full_name,
        state.number,
        task_id,
        status_value or "unchanged",
        ok,
    )
    return {
        "ok": ok,
        "plaky_task_id": task_id,
        "event": event_label,
        "task_type": state.task_type,
        "priority": state.priority,
        "status": status_value or None,
        "status_held_back": status_held_back or None,
        "mutation": mutation,
    }


# -- closed / reopened -----------------------------------------------------------------------


async def _transition(
    payload: IssueEventPayload,
    session: AsyncSession,
    *,
    target_intents: tuple[str, ...],
    literal_fallback: str,
    action_name: str,
    task_comment: str,
    resume_status: str = "",
    capture_previous: bool = False,
    client: ClickUpClient | None = None,
) -> dict[str, Any]:
    """Shared close/reopen flow: map the issue to its task, write a status, comment once."""
    c = client or ClickUpClient()
    repo_name = payload.repository.name
    number = payload.issue.number
    mapping = await _find_mapping(repo_name, number, session)
    task_id = _mapped_id(mapping)
    if not task_id or task_id.startswith("pending:"):
        return {"ok": True, "skipped": True, "message": "no ClickUp task mapped for this issue"}

    def _by_intent() -> str:
        for intent in target_intents:
            name = status_for_intent(intent)
            if name:
                return name
        return (literal_fallback or "").strip()

    target = (resume_status or "").strip() or _by_intent()
    if not target:
        return {
            "ok": True,
            "skipped": True,
            "message": f"no status resolvable for {action_name} (set the CLICKUP_STATUS_* settings)",
        }

    previous = ""
    if capture_previous:
        got = await c.get_task(task_id)
        if got.get("ok"):
            previous = _current_status(got["task"])
        if previous.casefold() == target.casefold():
            previous = ""  # already at the target; nothing worth resuming

    res = await c.update_task_fields(task_id, status=target)
    if resume_status and not res.get("ok"):
        # The remembered status may no longer exist on the list: fall back to the intent ladder
        # instead of leaving the task stuck on Completed.
        fallback = _by_intent()
        if fallback and fallback != target:
            target = fallback
            res = await c.update_task_fields(task_id, status=target)
    res.pop("task", None)

    comment = await mirror_github_activity(
        session,
        c,
        task_id=task_id,
        action=f"{action_name}_comment",
        # The GitHub timestamp makes this per occurrence: a close, reopen, close cycle announces
        # the second close too.
        marker=(
            f"github:issue-state:{repo_name}:{number}:{action_name}"
            f":{str(getattr(payload.issue, 'updated_at', '') or '').strip()}"
        ),
        body=task_comment,
        github_repo=repo_name,
        github_ref=str(number),
    )
    detail: dict[str, Any] = {
        "issue_url": payload.issue.html_url,
        "clickup_status": target,
        "comment_ok": comment.get("ok"),
    }
    if capture_previous:
        detail["previous_status_value"] = previous
        detail["previous_status_key"] = ""
        detail["captured_previous"] = bool(previous)
    session.add(
        SyncLog(
            action=action_name,
            github_repo=repo_name,
            github_ref=str(number),
            plaky_task_id=task_id,
            detail=json.dumps(detail, default=str),
        )
    )
    await session.commit()
    return {"ok": bool(res.get("ok")), "plaky_task_id": task_id, "status": target, "clickup": res}


async def handle_issue_closed(
    payload: IssueEventPayload, session: AsyncSession, *, client: ClickUpClient | None = None
) -> dict[str, Any]:
    """Issue closed: mark the task complete, remembering the status it held for a reopen."""
    return await _transition(
        payload,
        session,
        target_intents=("workflow_completed",),
        literal_fallback=settings.clickup_status_completed,
        action_name="issue_closed",
        task_comment=(
            f"**Issue closed on GitHub:** #{payload.issue.number} "
            "- task marked complete by automation."
        ),
        capture_previous=True,
        client=client,
    )


async def handle_issue_reopened(
    payload: IssueEventPayload, session: AsyncSession, *, client: ClickUpClient | None = None
) -> dict[str, Any]:
    """Issue reopened: resume where the task left off.

    An owned issue restores the exact status recorded at close. An unowned one always goes to
    "needs assigned", so a reopened task never shows a working status with nobody on it.
    """
    from boardman.services.issue_handler import _issue_assignee_login, _pre_close_status

    number = payload.issue.number
    has_owner = bool(_issue_assignee_login(payload.issue))
    resume = ""
    if has_owner:
        mapping = await _find_mapping(payload.repository.name, number, session)
        if _mapped_id(mapping):
            remembered = await _pre_close_status(session, _mapped_id(mapping))
            resume = remembered[1] if remembered else ""
    return await _transition(
        payload,
        session,
        target_intents=("workflow_assigned",) if has_owner else ("workflow_needs_assigned",),
        literal_fallback="",
        action_name="issue_reopened",
        task_comment=f"**Issue reopened on GitHub:** #{number} - task resumed by automation.",
        resume_status=resume,
        client=client,
    )
