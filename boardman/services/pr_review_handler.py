"""GitHub PR reviews + PR comments → Plaky QA statuses."""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from boardman.assignment.config import TeamAssignmentsConfig, load_team_assignments
from boardman.database.models import SyncLog
from boardman.github.repo_fetch import fetch_pr_assignees_and_reviewers_logins
from boardman.github.support_qa import support_team_logins_casefold
from boardman.github.webhooks import IssueCommentEventPayload, PullRequestReviewEventPayload
from boardman.plaky.client import PlakyClient
from boardman.plaky.dynamic_qa_status import (
    github_actor_payload,
    resolve_github_user_to_plaky_user_id,
    resolve_plaky_status_patch,
    resolve_qa_assignee_field_key,
)
from boardman.repos_config import get_routing
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


async def handle_pull_request_review(
    payload: PullRequestReviewEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    if payload.action != "submitted":
        return {"ok": True, "message": "ignored non-submitted review"}

    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    routing = get_routing(payload.repository.full_name, repo_name, settings.github_org)
    board_id = (routing.plaky_board_id if routing and routing.plaky_board_id else "") or ""

    task_ids = await _task_ids_for_pr(session, repo_name, pr_number)
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no Plaky task linked for this PR"}

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
    board_id = (routing.plaky_board_id if routing and routing.plaky_board_id else "") or ""

    task_ids = await _task_ids_for_pr(session, repo_name, pr_number)
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no Plaky task linked for this PR"}

    comment_user: dict[str, Any] = {}
    commenter = ""
    if isinstance(payload.comment, dict):
        u = payload.comment.get("user")
        if isinstance(u, dict):
            comment_user = u
            commenter = str(u.get("login") or "").strip()

    bid = (board_id or "").strip()
    in_qa = _in_qa_status()
    in_qa_field_key: Optional[str] = None
    if not in_qa and bid:
        r = await resolve_plaky_status_patch(bid, intent="workflow_in_qa")
        if r:
            in_qa_field_key, in_qa = r[0], r[1]
    if not in_qa:
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
