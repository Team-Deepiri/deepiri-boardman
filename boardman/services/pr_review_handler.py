"""GitHub PR reviews + PR comments → Plaky QA statuses."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import SyncLog
from boardman.github.repo_fetch import fetch_pr_assignees_and_reviewers_logins
from boardman.github.support_qa import support_team_logins_casefold
from boardman.github.webhooks import IssueCommentEventPayload, PullRequestReviewEventPayload
from boardman.plaky.client import PlakyClient
from boardman.services.pr_task_registry import distinct_task_ids_for_pr
from boardman.services.webhook_side_effects import maybe_enqueue_plaky_reorder_job
from boardman.settings import settings


async def _task_ids_for_pr(session: AsyncSession, repo_name: str, pr_number: int) -> list[str]:
    return await distinct_task_ids_for_pr(session, github_repo=repo_name, github_pr_number=pr_number)


async def handle_pull_request_review(
    payload: PullRequestReviewEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    if payload.action != "submitted":
        return {"ok": True, "message": "ignored non-submitted review"}

    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    task_ids = await _task_ids_for_pr(session, repo_name, pr_number)
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no Plaky task linked for this PR"}

    reviewer_login = ""
    if isinstance(payload.review.user, dict):
        reviewer_login = str(payload.review.user.get("login") or "").strip()

    state = (payload.review.state or "").strip().casefold()
    support = support_team_logins_casefold()
    on_support_roster = bool(reviewer_login) and reviewer_login.casefold() in support

    plaky = PlakyClient()
    updated: list[dict[str, Any]] = []

    target_status = ""
    if on_support_roster:
        if state == "approved" and (settings.plaky_pr_qa_approved_status or "").strip():
            target_status = settings.plaky_pr_qa_approved_status.strip()
        elif state == "changes_requested" and (settings.plaky_pr_qa_rejected_status or "").strip():
            target_status = settings.plaky_pr_qa_rejected_status.strip()
        elif state == "commented" and (settings.plaky_pr_in_qa_status or "").strip():
            target_status = settings.plaky_pr_in_qa_status.strip()

    if not target_status:
        return {
            "ok": True,
            "skipped": True,
            "message": "no matching QA status for this review",
            "reviewer": reviewer_login,
            "state": state,
        }

    for tid in task_ids:
        res = await plaky.update_task_status(tid, target_status)
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
                    "plaky_ok": res.get("ok"),
                },
                default=str,
            ),
        )
        session.add(log)
        updated.append({"task_id": tid, "plaky": res})

    await session.commit()
    await maybe_enqueue_plaky_reorder_job()
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
    task_ids = await _task_ids_for_pr(session, repo_name, pr_number)
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no Plaky task linked for this PR"}

    commenter = ""
    if isinstance(payload.comment, dict):
        u = payload.comment.get("user")
        if isinstance(u, dict):
            commenter = str(u.get("login") or "").strip()

    in_qa = (settings.plaky_pr_in_qa_status or "").strip()
    if not in_qa:
        return {"ok": True, "skipped": True, "message": "plaky_pr_in_qa_status not configured"}

    participants = await fetch_pr_assignees_and_reviewers_logins(
        payload.repository.full_name,
        pr_number,
    )
    if participants and commenter.casefold() not in participants:
        return {
            "ok": True,
            "skipped": True,
            "message": "commenter is not an assignee or requested reviewer",
            "commenter": commenter,
        }

    plaky = PlakyClient()
    updated: list[dict[str, Any]] = []
    for tid in task_ids:
        res = await plaky.update_task_status(tid, in_qa)
        log = SyncLog(
            action="pr_comment_in_qa",
            github_repo=repo_name,
            github_ref=str(pr_number),
            plaky_task_id=tid,
            detail=json.dumps({"commenter": commenter, "plaky_status": in_qa, "plaky_ok": res.get("ok")}),
        )
        session.add(log)
        updated.append({"task_id": tid, "plaky": res})

    await session.commit()
    await maybe_enqueue_plaky_reorder_job()
    return {"ok": True, "updated": updated, "status": in_qa}
