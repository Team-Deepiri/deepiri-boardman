"""PR handling for GitHub webhooks: opened, merged, reviews, etc."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from boardman.assignment.config import load_team_assignments
from boardman.assignment.qa_picker import pick_qa_for_repo
from boardman.assignment.team_checker import is_support_member
from boardman.database.models import SyncLog
from boardman.github.webhooks import PullRequestEventPayload
from boardman.github.webhooks import PullRequestReviewEventPayload, PullRequestReviewCommentEventPayload
from boardman.plaky.board_schema import fetch_board_schema_bundle
from boardman.plaky.client import PlakyClient
from boardman.services.issue_handler import find_plaky_task_by_issue, get_linked_issue_numbers
from boardman.services.pr_task_linking import (
    format_triage_comment,
    run_pr_task_pipeline,
    should_run_pipeline,
)
from boardman.services.pr_task_registry import (
    distinct_task_ids_for_pr,
    has_any_open_pr_for_task,
    mark_pr_merged,
    mark_pr_withdrawn,
    upsert_pr_task_link,
)
from boardman.services.webhook_side_effects import maybe_enqueue_plaky_reorder_job
from boardman.services.pr_tracker import upsert_pr_row, remove_pr_row
from boardman.settings import settings

_log = logging.getLogger(__name__)


async def _update_plaky_task_status(
    plaky: PlakyClient,
    task_id: str,
    status_value: str,
    board_id: str,
) -> dict:
    """Update task status using board schema to find the status field key."""
    if not board_id:
        return await plaky.update_task_status(task_id, status_value)
    
    schema_bundle = await fetch_board_schema_bundle(board_id)
    if not schema_bundle.get("ok") or not schema_bundle.get("normalized"):
        return await plaky.update_task_status(task_id, status_value)
    
    normalized = schema_bundle["normalized"]
    fields = normalized.get("fields") or []
    
    status_field_key = None
    for f in fields:
        ftype = (f.get("type") or "").lower()
        fname = (f.get("name") or "").lower()
        if "status" in ftype or "status" in fname:
            status_field_key = f.get("key")
            break
    
    if not status_field_key:
        return await plaky.update_task_status(task_id, status_value)
    
    return await plaky.patch_item_field_values(board_id, task_id, {status_field_key: status_value})


async def _maybe_set_needs_qa(plaky: PlakyClient, task_id: str, is_draft: bool) -> None:
    st = (settings.plaky_pr_needs_qa_status or "").strip()
    if not st:
        return
    if is_draft and settings.plaky_skip_needs_qa_for_draft:
        return
    await plaky.update_task_status(task_id, st)
    await maybe_enqueue_plaky_reorder_job()


async def _maybe_triage_ambiguous_pr(
    payload: PullRequestEventPayload,
    session: AsyncSession,
) -> dict[str, Any] | None:
    """
    PRs with no Fixes/Closes issue link: optional Plaky triage task + QA assignee.
    Configure under `ambiguous_pr` in team_assignments.yml.
    """
    cfg = load_team_assignments()
    amb = cfg.ambiguous_pr
    if not amb.enabled:
        return None
    bid = (amb.triage_board_id or "").strip()
    gid = (amb.triage_group_id or "").strip()
    if not bid or not gid:
        return {
            "ok": True,
            "skipped": True,
            "message": "ambiguous_pr enabled but triage_board_id / triage_group_id missing",
        }

    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    pr_url = payload.pull_request.html_url
    full_name = payload.repository.full_name
    title = amb.title_template.format(number=pr_number, repo=repo_name, full_name=full_name)
    description = (
        f"GitHub PR (no linked issue): {pr_url}\n\n"
        f"**Repo:** `{full_name}`\n\n"
        "This PR did not reference an issue with `Fixes #` / `Closes #` / `Resolves #`. "
        "Triage: link the right issue, add QA plan, or split work.\n"
    )

    field_values: dict[str, str] = {}
    if amb.assign_qa:
        qid, _ = await pick_qa_for_repo(full_name)
        if qid and cfg.plaky_field_qa:
            field_values[cfg.plaky_field_qa] = qid

    plaky = PlakyClient()
    res = await plaky.create_task(
        title=title,
        description=description,
        priority="medium",
        board_id=bid,
        group_id=gid,
        field_values=field_values if field_values else None,
    )
    if not res.get("ok"):
        return {"ok": False, "message": res.get("message"), "ambiguous_triage": True}

    task_id = res.get("task", {}).get("id") or res.get("task", {}).get("taskId")
    log = SyncLog(
        action="pr_ambiguous_triage",
        github_repo=repo_name,
        github_ref=str(pr_number),
        plaky_task_id=task_id,
        detail=json.dumps({"pr_url": pr_url, "full_name": full_name}),
    )
    session.add(log)
    await session.commit()
    return {
        "ok": True,
        "ambiguous_triage": True,
        "plaky_task_id": task_id,
        "plaky_task_url": res.get("task_url"),
    }


async def handle_pr_opened(payload: PullRequestEventPayload, session: AsyncSession) -> dict:
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    pr_url = payload.pull_request.html_url
    is_draft = bool(payload.pull_request.draft)
    full_name = payload.repository.full_name

    linked_issues = await get_linked_issue_numbers(payload.pull_request.body)

    from boardman.repos_config import get_routing
    routing = get_routing(full_name, repo_name, settings.github_org)
    board_id = routing.plaky_board_id if routing and routing.plaky_board_id else settings.plaky_default_board_id

    plaky = PlakyClient()
    results = []

    is_draft = payload.pull_request.draft

    if not is_draft:
        tracker_result = await upsert_pr_row(payload.pull_request, payload.repository, session)
        if tracker_result.get("ok"):
            _log.debug("PR tracking row upserted: %s", tracker_result)

    for issue_num in linked_issues:
        mapping = await find_plaky_task_by_issue(repo_name, issue_num, session)
        if mapping:
            await upsert_pr_task_link(
                session,
                github_repo=repo_name,
                github_pr_number=pr_number,
                plaky_task_id=mapping.plaky_task_id,
                github_issue_number=int(issue_num),
                link_source="issue_keyword",
            )
            if not is_draft:
                await _update_plaky_task_status(plaky, mapping.plaky_task_id, settings.plaky_status_needs_qa, board_id)

            comment = f"**PR Opened:** [{pr_number}]({pr_url})"
            await plaky.add_comment(mapping.plaky_task_id, comment)
            await _maybe_set_needs_qa(plaky, mapping.plaky_task_id, is_draft)

            log = SyncLog(
                action="pr_linked",
                github_repo=repo_name,
                github_ref=str(pr_number),
                plaky_task_id=mapping.plaky_task_id,
                detail=json.dumps({"issue_number": issue_num, "pr_url": pr_url}),
            )
            session.add(log)
            results.append({"issue": issue_num, "task_id": mapping.plaky_task_id})

    if not linked_issues:
        run_pipe = settings.pr_linking_pipeline_enabled and await should_run_pipeline(
            payload.pull_request.body
        )
        if run_pipe:
            pr_user = payload.pull_request.user or {}
            pr_author_login = pr_user.get("login") if isinstance(pr_user, dict) else None
            pr_author_email = pr_user.get("email") if isinstance(pr_user, dict) else None
            pr_author_name = pr_user.get("name") if isinstance(pr_user, dict) else None

            pipe = await run_pr_task_pipeline(
                session=session,
                plaky=plaky,
                repo_full=payload.repository.full_name,
                repo_name=repo_name,
                org=settings.github_org,
                pr_number=pr_number,
                pr_title=payload.pull_request.title,
                pr_body=payload.pull_request.body,
                head=payload.pull_request.head,
                pr_author_login=pr_author_login,
                pr_author_email=pr_author_email,
                pr_author_name=pr_author_name,
            )
            plog = SyncLog(
                action="pr_link_pipeline",
                github_repo=repo_name,
                github_ref=str(pr_number),
                plaky_task_id=pipe.task_id,
                detail=json.dumps(
                    {
                        "decision": pipe.decision,
                        "score": pipe.score,
                        "reason": pipe.reason,
                        "detail": pipe.log_detail,
                        "triage_comment": format_triage_comment(pipe.top_scored)
                        if pipe.decision == "triage"
                        else None,
                    },
                    default=str,
                ),
            )
            session.add(plog)

            if pipe.decision in ("auto_link", "llm_link") and pipe.task_id:
                await upsert_pr_task_link(
                    session,
                    github_repo=repo_name,
                    github_pr_number=pr_number,
                    plaky_task_id=pipe.task_id,
                    github_issue_number=0,
                    link_source=pipe.decision,
                )
                comment = (
                    f"**PR Opened** (automation link — {pipe.decision}): "
                    f"[{pr_number}]({pr_url})"
                )
                await plaky.add_comment(pipe.task_id, comment)
                await _maybe_set_needs_qa(plaky, pipe.task_id, is_draft)
                log = SyncLog(
                    action="pr_linked_fuzzy",
                    github_repo=repo_name,
                    github_ref=str(pr_number),
                    plaky_task_id=pipe.task_id,
                    detail=json.dumps(
                        {"pr_url": pr_url, "pipeline": pipe.decision, "score": pipe.score},
                        default=str,
                    ),
                )
                session.add(log)
                await session.commit()
                return {
                    "ok": True,
                    "linked": [{"task_id": pipe.task_id, "via": pipe.decision}],
                    "pipeline": pipe.decision,
                }

            await session.commit()

        triage = await _maybe_triage_ambiguous_pr(payload, session)
        if triage is not None:
            return triage
        return {"ok": True, "skipped": True, "message": "No linked issues found"}

    await session.commit()
    return {"ok": True, "linked": results}


async def handle_pr_ready_for_review(
    payload: PullRequestEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    """Draft → ready: move linked Plaky tasks to Needs QA when configured."""
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    plaky = PlakyClient()
    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if not task_ids:
        linked_issues = await get_linked_issue_numbers(payload.pull_request.body)
        for issue_num in linked_issues:
            mapping = await find_plaky_task_by_issue(repo_name, issue_num, session)
            if mapping:
                await upsert_pr_task_link(
                    session,
                    github_repo=repo_name,
                    github_pr_number=pr_number,
                    plaky_task_id=mapping.plaky_task_id,
                    github_issue_number=int(issue_num),
                    link_source="issue_keyword",
                )
                task_ids.append(mapping.plaky_task_id)
        task_ids = list(dict.fromkeys(task_ids))

    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no linked Plaky tasks for this PR"}

    for tid in task_ids:
        await _maybe_set_needs_qa(plaky, tid, is_draft=False)

    await session.commit()
    return {"ok": True, "tasks": task_ids, "event": "ready_for_review"}


async def handle_pr_review_requested(
    payload: PullRequestEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    in_qa = (settings.plaky_pr_in_qa_status or "").strip()
    if not in_qa:
        return {"ok": True, "skipped": True, "message": "plaky_pr_in_qa_status not configured"}

    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no linked Plaky tasks for this PR"}

    plaky = PlakyClient()
    for tid in task_ids:
        await plaky.update_task_status(tid, in_qa)
    await session.commit()
    await maybe_enqueue_plaky_reorder_job()
    return {"ok": True, "tasks": task_ids, "status": in_qa, "event": "review_requested"}


async def handle_pr_closed_without_merge(
    payload: PullRequestEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    rows = await mark_pr_withdrawn(session, github_repo=repo_name, github_pr_number=pr_number)
    log = SyncLog(
        action="pr_closed_without_merge",
        github_repo=repo_name,
        github_ref=str(pr_number),
        plaky_task_id=None,
        detail=json.dumps({"withdrawn_links": len(rows)}),
    )
    session.add(log)
    await session.commit()
    return {"ok": True, "withdrawn_links": len(rows)}


async def handle_pr_merged(payload: PullRequestEventPayload, session: AsyncSession) -> dict:
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    pr_url = payload.pull_request.html_url

    linked_issues = await get_linked_issue_numbers(payload.pull_request.body)

    plaky = PlakyClient()
    for issue_num in linked_issues:
        mapping = await find_plaky_task_by_issue(repo_name, issue_num, session)
        if mapping:
            await upsert_pr_task_link(
                session,
                github_repo=repo_name,
                github_pr_number=pr_number,
                plaky_task_id=mapping.plaky_task_id,
                github_issue_number=int(issue_num),
                link_source="issue_keyword",
            )

    merged_rows = await mark_pr_merged(session, github_repo=repo_name, github_pr_number=pr_number)

    affected_tasks: set[str] = {row.plaky_task_id for row in merged_rows}
    results: list[dict[str, Any]] = []

    for issue_num in linked_issues:
        mapping = await find_plaky_task_by_issue(repo_name, issue_num, session)
        if mapping:
            board_id = settings.plaky_default_board_id
            affected_tasks.add(mapping.plaky_task_id)
            await _update_plaky_task_status(plaky, mapping.plaky_task_id, settings.plaky_status_completed, board_id)

            merge_detail = {
                "issue_number": issue_num,
                "pr_url": pr_url,
                "status": settings.plaky_status_completed,
            }
            log = SyncLog(
                action="pr_merged_issue",
                github_repo=repo_name,
                github_ref=str(pr_number),
                plaky_task_id=mapping.plaky_task_id,
                detail=json.dumps(merge_detail),
            )
            session.add(log)

    if not affected_tasks:
        await session.commit()
        return {"ok": True, "skipped": True, "message": "No linked Plaky tasks for this PR"}

    merge_status = (settings.plaky_pr_merge_status or "").strip() or "in_review"

    for task_id in sorted(affected_tasks):
        if settings.plaky_complete_when_all_prs_merged and await has_any_open_pr_for_task(
            session, plaky_task_id=task_id
        ):
            results.append(
                {
                    "issue": issue_num,
                    "task_id": task_id,
                    "status": settings.plaky_status_completed,
                    "deferred": True,
                    "reason": "other_prs_still_open_or_active",
                }
            )
            continue

        await plaky.update_task_status(task_id, merge_status)
        merge_detail = {"pr_url": pr_url, "status": merge_status, "all_prs_done": True}
        log = SyncLog(
            action="pr_merged",
            github_repo=repo_name,
            github_ref=str(pr_number),
            plaky_task_id=task_id,
            detail=json.dumps(merge_detail),
        )
        session.add(log)
        results.append({"task_id": task_id, "status": merge_status})
        await maybe_enqueue_plaky_reorder_job()

    await remove_pr_row(payload.pull_request, payload.repository, session)

    await session.commit()
    return {"ok": True, "updated": results}


async def handle_pr_review(payload: PullRequestReviewEventPayload, session: AsyncSession) -> dict:
    """Handle PR review submitted events - update Plaky status based on review outcome."""
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    pr_url = payload.pull_request.html_url
    review = payload.review
    reviewer = review.user
    reviewer_login = reviewer.get("login") if isinstance(reviewer, dict) else None

    if not reviewer_login:
        return {"ok": False, "message": "No reviewer login found"}

    linked_issues = await get_linked_issue_numbers(payload.pull_request.body)
    if not linked_issues:
        return {"ok": True, "skipped": True, "message": "No linked issues found"}

    plaky = PlakyClient()
    results = []

    is_support = is_support_member(reviewer_login)
    cfg = load_team_assignments()
    qa_field = cfg.plaky_field_qa

    for issue_num in linked_issues:
        mapping = await find_plaky_task_by_issue(repo_name, issue_num, session)
        if not mapping:
            continue

        status_to_set = None
        action = ""

        if is_support:
            if review.state == "approved":
                status_to_set = settings.plaky_status_qa_approved
                action = "qa_approved"
            elif review.state == "changes_requested":
                status_to_set = settings.plaky_status_qa_rejected
                action = "qa_rejected"
        elif qa_field:
            assigned_qa_id = ""
            task_info = await plaky.get_board_item_public(settings.plaky_default_board_id or "", mapping.plaky_task_id)
            if task_info.get("ok") and task_info.get("item"):
                item = task_info.get("item", {})
                current_qa = item.get(qa_field)
                if current_qa and isinstance(current_qa, dict):
                    assigned_qa_id = str(current_qa.get("id", ""))
                else:
                    assigned_qa_id = str(current_qa) if current_qa else ""

            reviewer_plaky_id = None
            for m in cfg.members:
                if m.github_login.lower() == reviewer_login.lower():
                    reviewer_plaky_id = m.id
                    break

            if assigned_qa_id and reviewer_plaky_id and assigned_qa_id == reviewer_plaky_id:
                status_to_set = settings.plaky_status_in_qa
                action = "in_qa"

        if status_to_set:
            board_id = settings.plaky_default_board_id
            await _update_plaky_task_status(plaky, mapping.plaky_task_id, status_to_set, board_id)
            comment = f"**PR Review:** [{review.state}]({pr_url}) by @{reviewer_login}"
            await plaky.add_comment(mapping.plaky_task_id, comment)

            log = SyncLog(
                action=action,
                github_repo=repo_name,
                github_ref=str(pr_number),
                plaky_task_id=mapping.plaky_task_id,
                detail=json.dumps({
                    "issue_number": issue_num,
                    "pr_url": pr_url,
                    "reviewer": reviewer_login,
                    "review_state": review.state,
                    "status": status_to_set,
                }),
            )
            session.add(log)
            results.append({
                "issue": issue_num,
                "task_id": mapping.plaky_task_id,
                "action": action,
                "status": status_to_set,
            })

    await session.commit()
    return {"ok": True, "updated": results}


async def handle_pr_review_comment(payload: PullRequestReviewCommentEventPayload, session: AsyncSession) -> dict:
    """Handle PR review comment events - mark as In QA if commenter is assigned QA."""
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number if payload.pull_request else 0
    pr_url = payload.pull_request.html_url if payload.pull_request else ""

    comment = payload.comment
    commenter = comment.get("user") if isinstance(comment, dict) else None
    commenter_login = commenter.get("login") if isinstance(commenter, dict) else None

    if not commenter_login:
        return {"ok": False, "message": "No commenter login found"}

    linked_issues = await get_linked_issue_numbers(payload.pull_request.body if payload.pull_request else None)
    if not linked_issues:
        return {"ok": True, "skipped": True, "message": "No linked issues found"}

    cfg = load_team_assignments()
    qa_field = cfg.plaky_field_qa

    if not qa_field:
        return {"ok": True, "skipped": True, "message": "QA field not configured"}

    plaky = PlakyClient()
    results = []

    for issue_num in linked_issues:
        mapping = await find_plaky_task_by_issue(repo_name, issue_num, session)
        if not mapping:
            continue

        task_info = await plaky.get_board_item_public(settings.plaky_default_board_id or "", mapping.plaky_task_id)
        if not task_info.get("ok") or not task_info.get("item"):
            continue

        item = task_info.get("item", {})
        current_qa = item.get(qa_field)
        if current_qa and isinstance(current_qa, dict):
            assigned_qa_id = str(current_qa.get("id", ""))
        else:
            assigned_qa_id = str(current_qa) if current_qa else ""

        reviewer_plaky_id = None
        for m in cfg.members:
            if m.github_login.lower() == commenter_login.lower():
                reviewer_plaky_id = m.id
                break

        if assigned_qa_id and reviewer_plaky_id and assigned_qa_id == reviewer_plaky_id:
            status_to_set = settings.plaky_status_in_qa
            board_id = settings.plaky_default_board_id
            await _update_plaky_task_status(plaky, mapping.plaky_task_id, status_to_set, board_id)
            comment_text = f"**PR Comment:** by @{commenter_login}"
            await plaky.add_comment(mapping.plaky_task_id, comment_text)

            log = SyncLog(
                action="in_qa_comment",
                github_repo=repo_name,
                github_ref=str(pr_number),
                plaky_task_id=mapping.plaky_task_id,
                detail=json.dumps({
                    "issue_number": issue_num,
                    "pr_url": pr_url,
                    "commenter": commenter_login,
                    "status": status_to_set,
                }),
            )
            session.add(log)
            results.append({
                "issue": issue_num,
                "task_id": mapping.plaky_task_id,
                "action": "in_qa_comment",
                "status": status_to_set,
            })

    await session.commit()
    return {"ok": True, "updated": results}
