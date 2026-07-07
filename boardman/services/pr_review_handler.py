"""GitHub PR reviews + PR comments → Plaky QA statuses."""

from __future__ import annotations

import json
from typing import Any
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from boardman.assignment.config import TeamAssignmentsConfig, load_team_assignments
from boardman.database.models import SyncLog
from boardman.github.repo_fetch import fetch_pr_assignees_and_reviewers_logins
from boardman.github.support_qa import support_team_logins_casefold
from boardman.github.webhooks import IssueCommentEventPayload, PullRequestReviewEventPayload
from boardman.plaky.board_schema import plaky_item_person_ids, plaky_item_status_id
from boardman.plaky.client import PlakyClient
from boardman.plaky.dynamic_qa_status import (
    github_actor_payload,
    resolve_github_user_to_plaky_user_id,
    resolve_plaky_status_patch,
    resolve_qa_assignee_field_key,
)
from boardman.repos_config import get_routing
from boardman.repos_config import get_routing_async main
from boardman.services.pr_handler import _update_plaky_task_status
from boardman.services.pr_task_registry import distinct_task_ids_for_pr
from boardman.services.webhook_side_effects import maybe_enqueue_plaky_reorder_after_task
from boardman.settings import settings


def _qa_approved_status() -> str:
    return (settings.plaky_pr_qa_approved_status or settings.plaky_status_qa_approved or "").strip()


def _qa_rejected_status() -> str:
    return (settings.plaky_pr_qa_rejected_status or settings.plaky_status_qa_rejected or "").strip()


def _in_qa_status() -> str:
    return (settings.plaky_pr_in_qa_status or settings.plaky_status_in_qa or "").strip()


async def _task_ids_for_pr(session: AsyncSession, repo_name: str, pr_number: int) -> list[str]:
    return await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )


def _reviewer_plaky_id_from_roster(cfg: TeamAssignmentsConfig, reviewer_login: str) -> str | None:
def _paused_status() -> str:
    return (settings.plaky_status_paused or "").strip()


def _in_progress_status() -> str:
    return (settings.plaky_status_in_progress or "").strip()


def _needs_qa_again_status() -> str:
    return (
        settings.plaky_status_needs_qa_again
        or settings.plaky_pr_needs_qa_status
        or settings.plaky_status_needs_qa
        or ""
    ).strip()


async def _resolve_status(board_id: str, env_value: str, *intents: str) -> tuple[Optional[str], str]:
    """Return (status_field_key, status_value). Prefer the env value; else try each schema intent."""
    if env_value:
        return None, env_value
    bid = (board_id or "").strip()
    if not bid:
        return None, ""
    for intent in intents:
        rp = await resolve_plaky_status_patch(bid, intent=intent)
        if rp:
            return rp[0], rp[1]
    return None, ""


async def _task_ids_for_pr(session: AsyncSession, repo_name: str, pr_number: int) -> list[str]:
    return await distinct_task_ids_for_pr(session, github_repo=repo_name, github_pr_number=pr_number)


def _reviewer_plaky_id_from_roster(cfg: TeamAssignmentsConfig, reviewer_login: str) -> Optional[str]:
    if not reviewer_login:
        return None
    for m in cfg.members:
        gl = (getattr(m, "github_login", None) or "").strip()
        if gl and gl.casefold() == reviewer_login.casefold():
            mid = (getattr(m, "id", None) or "").strip()
            return mid or None
    return None


async def _assigned_qa_plaky_id(
    plaky: PlakyClient,
    board_id: str,
    task_id: str,
    qa_field: str,
) -> str:
    task_info = await plaky.get_board_item_public((board_id or "").strip(), task_id)
    if not task_info.get("ok") or not task_info.get("item"):
        return ""
    item = task_info["item"]
    current_qa = item.get(qa_field)
    if isinstance(current_qa, dict):
        return str(current_qa.get("id") or "")
    return str(current_qa or "") if current_qa else ""
    ids = plaky_item_person_ids(task_info["item"], qa_field)
    return ids[0] if ids else ""


async def handle_pull_request_review(
    payload: PullRequestReviewEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    if payload.action != "submitted":
        return {"ok": True, "message": "ignored non-submitted review"}

    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    routing = get_routing(payload.repository.full_name, repo_name, settings.github_org)
    routing = await get_routing_async(payload.repository.full_name, repo_name, settings.github_org)
    board_id = (routing.plaky_board_id if routing and routing.plaky_board_id else "") or ""

    task_ids = await _task_ids_for_pr(session, repo_name, pr_number)
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no Plaky task linked for this PR"}

    review_user: dict[str, Any] = (
        payload.review.user if isinstance(payload.review.user, dict) else {}
    )
    review_user: dict[str, Any] = payload.review.user if isinstance(payload.review.user, dict) else {}
    reviewer_login = str(review_user.get("login") or "").strip()

    state = (payload.review.state or "").strip().casefold()
    support = support_team_logins_casefold()
    on_support_roster = bool(reviewer_login) and reviewer_login.casefold() in support

    plaky = PlakyClient()
    updated: list[dict[str, Any]] = []

    # Approved: any reviewer's Approve → QA verified / approved Plaky status.
    # changes_requested: only the Plaky-assigned QA's "Request changes" → QA rejected (not other reviewers).
    # "commented" → In QA only for support-team logins to avoid noise from drive-by comments.
    target_status = ""
    status_field_key: str | None = None
    bid = (board_id or "").strip()

    changes_requested_only_assigned_qa = False
    reviewer_plaky_id: str | None = None
    status_field_key: Optional[str] = None
    bid = (board_id or "").strip()

    changes_requested_only_assigned_qa = False
    reviewer_plaky_id: Optional[str] = None
    qa_field_for_changes: str = ""

    if state == "approved":
        target_status = _qa_approved_status()
        if not target_status and bid:
            res = await resolve_plaky_status_patch(bid, intent="github_pr_review_approved")
            if res:
                status_field_key, target_status = res[0], res[1]
    elif state == "changes_requested":
        changes_requested_only_assigned_qa = True
        target_status = _qa_rejected_status()
        if not target_status and bid:
            res = await resolve_plaky_status_patch(bid, intent="github_pr_review_changes_requested")
            if res:
                status_field_key, target_status = res[0], res[1]
        if target_status and bid:
            cfg = load_team_assignments()
            qa_field_for_changes = await resolve_qa_assignee_field_key(bid, cfg.plaky_field_qa)
            reviewer_plaky_id = _reviewer_plaky_id_from_roster(cfg, reviewer_login)
            if not reviewer_plaky_id:
                reviewer_plaky_id = await resolve_github_user_to_plaky_user_id(
                    github_actor_payload(review_user)
                )
    elif state == "commented" and on_support_roster:
        target_status = _in_qa_status()
        if not target_status and bid:
            res = await resolve_plaky_status_patch(bid, intent="workflow_in_qa")
            if res:
                status_field_key, target_status = res[0], res[1]

    if not target_status:
        return {
            "ok": True,
            "skipped": True,
            "message": "no matching QA status for this review (configure env or ensure board has matching status labels)",
            "reviewer": reviewer_login,
            "state": state,
        }

    if changes_requested_only_assigned_qa:
        if not qa_field_for_changes:
            return {
                "ok": True,
                "skipped": True,
                "message": "changes_requested ignored: QA assignee field not configured or discoverable on board",
                "reviewer": reviewer_login,
                "state": state,
            }
        if not reviewer_plaky_id:
            return {
                "ok": True,
                "skipped": True,
                "message": "changes_requested ignored: could not map reviewer to a Plaky user id",
                "reviewer": reviewer_login,
                "state": state,
            }

    for tid in task_ids:
        if changes_requested_only_assigned_qa:
            assigned_qa = await _assigned_qa_plaky_id(
                plaky, board_id or "", tid, qa_field_for_changes
            )
            assigned_qa = await _assigned_qa_plaky_id(plaky, board_id or "", tid, qa_field_for_changes)
            if not assigned_qa or assigned_qa != reviewer_plaky_id:
                continue

        res = await _update_plaky_task_status(
            tid, target_status, board_id or "", status_field_key=status_field_key
        )
        log = SyncLog(
            action="pr_review_plaky_status",
            github_repo=repo_name,
            github_ref=str(pr_number),
            plaky_task_id=tid,
            detail=json.dumps(
                {
                    "review_state": state,
                    "reviewer": reviewer_login,
                    "plaky_status": target_status,
                    "plaky_status_field_key": status_field_key,
                    "plaky_ok": res.get("ok"),
                    "assigned_qa_only": changes_requested_only_assigned_qa,
                },
                default=str,
            ),
        )
        session.add(log)
        updated.append({"task_id": tid, "plaky": res})

    if changes_requested_only_assigned_qa and not updated:
        await session.commit()
        return {
            "ok": True,
            "skipped": True,
            "message": "changes_requested ignored: reviewer is not the Plaky-assigned QA on linked task(s)",
            "reviewer": reviewer_login,
            "state": state,
            "updated": [],
        }

    await session.commit()
    if updated and task_ids:
        await maybe_enqueue_plaky_reorder_after_task(plaky, task_ids[0])
    return {"ok": True, "updated": updated, "status": target_status}


async def handle_issue_comment_on_pr(
    payload: IssueCommentEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    if payload.action != "created":
        return {"ok": True, "message": "ignored non-created comment"}

    if not payload.issue.pull_request:
        return {"ok": True, "skipped": True, "message": "not a pull request comment"}

    repo_name = payload.repository.name
    pr_number = payload.issue.number
    routing = get_routing(payload.repository.full_name, repo_name, settings.github_org)
    routing = await get_routing_async(payload.repository.full_name, repo_name, settings.github_org)
    board_id = (routing.plaky_board_id if routing and routing.plaky_board_id else "") or ""

    task_ids = await _task_ids_for_pr(session, repo_name, pr_number)
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no Plaky task linked for this PR"}

    comment_user: dict[str, Any] = {}
    commenter = ""
    comment_body = ""
    if isinstance(payload.comment, dict):
        u = payload.comment.get("user")
        if isinstance(u, dict):
            comment_user = u
            commenter = str(u.get("login") or "").strip()

    bid = (board_id or "").strip()
    in_qa = _in_qa_status()
    in_qa_field_key: str | None = None
        comment_body = str(payload.comment.get("body") or "")

    bid = (board_id or "").strip()

    # --- Pause: any commenter saying "pause"/"paused"/"on hold" pauses the work. ---
    from boardman.github.pr_signals import comment_mentions_qa_or_support, comment_requests_pause

    if comment_requests_pause(comment_body):
        p_key, p_val = await _resolve_status(bid, _paused_status(), "workflow_paused")
        if not p_val:
            return {"ok": True, "skipped": True, "message": "pause requested but no paused status resolvable"}
        plaky_p = PlakyClient()
        updated_p: list[dict[str, Any]] = []
        for tid in task_ids:
            res = await _update_plaky_task_status(tid, p_val, board_id or "", status_field_key=p_key)
            session.add(
                SyncLog(
                    action="pr_comment_paused",
                    github_repo=repo_name,
                    github_ref=str(pr_number),
                    plaky_task_id=tid,
                    detail=json.dumps({"commenter": commenter, "plaky_status": p_val}, default=str),
                )
            )
            updated_p.append({"task_id": tid, "plaky": res})
        await session.commit()
        if updated_p:
            await maybe_enqueue_plaky_reorder_after_task(plaky_p, task_ids[0])
        return {"ok": True, "updated": updated_p, "status": p_val, "event": "paused"}

    # --- Dev pinged QA / support team (@mention) → Needs QA (again). ---
    if "@" in comment_body:
        support = support_team_logins_casefold()
        commenter_is_qa_side = bool(commenter) and commenter.casefold() in support
        if not commenter_is_qa_side and comment_mentions_qa_or_support(comment_body, support):
            q_key, q_val = await _resolve_status(
                bid, _needs_qa_again_status(), "workflow_needs_qa_again", "workflow_needs_qa"
            )
            if q_val:
                plaky_q = PlakyClient()
                updated_q: list[dict[str, Any]] = []
                for tid in task_ids:
                    res = await _update_plaky_task_status(tid, q_val, board_id or "", status_field_key=q_key)
                    session.add(
                        SyncLog(
                            action="pr_comment_needs_qa_again",
                            github_repo=repo_name,
                            github_ref=str(pr_number),
                            plaky_task_id=tid,
                            detail=json.dumps({"commenter": commenter, "plaky_status": q_val}, default=str),
                        )
                    )
                    updated_q.append({"task_id": tid, "plaky": res})
                await session.commit()
                if updated_q:
                    await maybe_enqueue_plaky_reorder_after_task(plaky_q, task_ids[0])
                return {"ok": True, "updated": updated_q, "status": q_val, "event": "needs_qa_again"}

    in_qa = _in_qa_status()
    in_qa_field_key: Optional[str] = None
    if not in_qa and bid:
        r = await resolve_plaky_status_patch(bid, intent="workflow_in_qa")
        if r:
            in_qa_field_key, in_qa = r[0], r[1]
    if not in_qa:

        return {
            "ok": True,
            "skipped": True,
            "message": "in_qa status not configured or discoverable",
        }
        return {"ok": True, "skipped": True, "message": "in_qa status not configured or discoverable"}

    participants = await fetch_pr_assignees_and_reviewers_logins(
        payload.repository.full_name,
        pr_number,
    )
    participants_cf = {str(p).casefold() for p in participants} if participants else set()
    is_participant = bool(participants_cf) and commenter.casefold() in participants_cf

    cfg = load_team_assignments()
    qa_field = await resolve_qa_assignee_field_key(bid, cfg.plaky_field_qa)
    plaky = PlakyClient()

    member_plaky_id: str | None = None
    member_plaky_id: Optional[str] = None
    if commenter:
        for m in cfg.members:
            gl = (m.github_login or "").strip()
            if gl and gl.casefold() == commenter.casefold():
                member_plaky_id = m.id
                break
        if not member_plaky_id:
            member_plaky_id = await resolve_github_user_to_plaky_user_id(
                github_actor_payload(comment_user)
            )

    is_assigned_qa = False
    if qa_field and member_plaky_id:
        for tid in task_ids:
            task_info = await plaky.get_board_item_public(board_id or "", tid)
            if not task_info.get("ok") or not task_info.get("item"):
                continue
            item = task_info["item"]
            current_qa = item.get(qa_field)
            if isinstance(current_qa, dict):
                assigned_id = str(current_qa.get("id") or "")
            else:
                assigned_id = str(current_qa or "")
            if assigned_id == member_plaky_id:
                is_assigned_qa = True
                break

            assigned_ids = plaky_item_person_ids(task_info["item"], qa_field)
            if member_plaky_id in assigned_ids:
                is_assigned_qa = True
                break

    # --- Dev resuming after a QA rejection: a non-QA comment while the task is QA-rejected
    # moves it back to In Progress (instead of In QA). Only when the status field is known
    # from the board schema (rej_key set) so we can read & compare the current value. ---
    if not is_assigned_qa:
        rej_key, rej_val = await _resolve_status(
            bid, _qa_rejected_status(), "github_pr_review_changes_requested"
        )
        if rej_key and rej_val:
            ip_key, ip_val = await _resolve_status(bid, _in_progress_status(), "workflow_in_progress")
            if ip_val:
                resumed: list[dict[str, Any]] = []
                for tid in task_ids:
                    info = await plaky.get_board_item_public(board_id or "", tid)
                    if not info.get("ok") or not info.get("item"):
                        continue
                    cur_id = plaky_item_status_id(info["item"], rej_key)
                    if cur_id and cur_id == str(rej_val):
                        res = await _update_plaky_task_status(
                            tid, ip_val, board_id or "", status_field_key=ip_key
                        )
                        session.add(
                            SyncLog(
                                action="pr_comment_resumed_in_progress",
                                github_repo=repo_name,
                                github_ref=str(pr_number),
                                plaky_task_id=tid,
                                detail=json.dumps({"commenter": commenter, "to_status": ip_val}, default=str),
                            )
                        )
                        resumed.append({"task_id": tid, "plaky": res})
                if resumed:
                    await session.commit()
                    await maybe_enqueue_plaky_reorder_after_task(plaky, resumed[0]["task_id"])
                    return {"ok": True, "updated": resumed, "status": ip_val, "event": "resumed_after_rejection"}

    if not is_participant and not is_assigned_qa:
        return {
            "ok": True,
            "skipped": True,
            "message": "commenter is not an assignee, requested reviewer, or Plaky-assigned QA",
            "commenter": commenter,
        }

    updated: list[dict[str, Any]] = []
    for tid in task_ids:
        res = await _update_plaky_task_status(
            tid, in_qa, board_id or "", status_field_key=in_qa_field_key
        )
        log = SyncLog(
            action="pr_comment_in_qa",
            github_repo=repo_name,
            github_ref=str(pr_number),
            plaky_task_id=tid,
            detail=json.dumps(
                {
                    "commenter": commenter,
                    "plaky_status": in_qa,
                    "plaky_status_field_key": in_qa_field_key,
                    "plaky_ok": res.get("ok"),
                },
                default=str,
            ),
        )
        session.add(log)
        updated.append({"task_id": tid, "plaky": res})

    await session.commit()
    if updated and task_ids:
        await maybe_enqueue_plaky_reorder_after_task(plaky, task_ids[0])
    return {"ok": True, "updated": updated, "status": in_qa}
