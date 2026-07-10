"""PR handling for GitHub webhooks: opened, merged, reviews, etc."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.assignment.config import load_team_assignments
from boardman.assignment.qa_picker import pick_qa_for_repo
from boardman.database.models import SyncLog
from boardman.github.webhooks import PullRequestEventPayload, PullRequestReviewCommentEventPayload
from boardman.plaky.board_schema import plaky_item_person_ids, plaky_item_status_id
from boardman.plaky.client import PlakyClient
from boardman.services.issue_handler import find_plaky_task_by_issue, get_linked_issue_numbers
from boardman.services.pr_link_comment import format_pr_notice_with_url
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
from boardman.services.task_mutations import UpdateTaskInput, update_task_internal
from boardman.services.webhook_side_effects import maybe_enqueue_plaky_reorder_after_task
from boardman.services.pr_tracker import upsert_pr_row, remove_pr_row
from boardman.settings import settings

_log = logging.getLogger(__name__)


async def _update_plaky_task_status(
    task_id: str,
    status_value: str,
    board_id: str,
    *,
    status_field_key: Optional[str] = None,
) -> dict:
    """Apply status via the same path as PATCH /tasks (schema-aware field patch + legacy /tasks fallback)."""
    return await update_task_internal(
        task_id,
        UpdateTaskInput(
            status=status_value,
            plaky_board_id=board_id or None,
            status_plaky_field_key=status_field_key,
        ),
    )


def _needs_qa_status_value() -> str:
    return (settings.plaky_pr_needs_qa_status or settings.plaky_status_needs_qa or "").strip()


async def _current_status_value(
    plaky: PlakyClient, board_id: str, task_id: str, status_field_key: str
) -> str:
    """Return the current option id/value stored in a STATUS field on a task item ("" if unset)."""
    bid = (board_id or "").strip()
    fk = (status_field_key or "").strip()
    if not bid or not fk:
        return ""
    info = await plaky.get_board_item_public(bid, task_id)
    if not info.get("ok") or not info.get("item"):
        return ""
    return plaky_item_status_id(info["item"], fk)


async def _current_person_field_value(
    plaky: PlakyClient, board_id: str, task_id: str, field_key: str
) -> str:
    """Return the current Plaky user id in a person field on a task item ("" if unset)."""
    bid = (board_id or "").strip()
    fk = (field_key or "").strip()
    if not bid or not fk:
        return ""
    info = await plaky.get_board_item_public(bid, task_id)
    if not info.get("ok") or not info.get("item"):
        return ""
    ids = plaky_item_person_ids(info["item"], fk)
    return ids[0] if ids else ""


async def _apply_pr_type_and_assignee(
    plaky: PlakyClient,
    *,
    task_id: str,
    board_id: str,
    pull_request: Any,
    repo_full: str,
) -> dict[str, Any]:
    """On a confident PR↔task link: set Type from branch/labels, and fill the developer assignee
    (and move to "Assigned") when the task has no assignee yet.

    Per the workflow: similarity already corroborated the match; we only WRITE the assignee when
    the task currently has none — an existing assignee is never overwritten.
    """
    from boardman.github.pr_signals import infer_task_type_from_pr, pr_label_names
    from boardman.plaky.board_aware import board_person_field_keys
    from boardman.plaky.dynamic_qa_status import (
        github_actor_payload,
        resolve_github_user_to_plaky_user_id,
        resolve_plaky_status_patch,
    )

    out: dict[str, Any] = {}
    bid = (board_id or "").strip()

    head = getattr(pull_request, "head", None)
    head_ref = str(head.get("ref")) if isinstance(head, dict) else ""
    labels = pr_label_names(getattr(pull_request, "labels", None))
    canon_type = infer_task_type_from_pr(head_ref, labels)
    if canon_type:
        res = await update_task_internal(
            task_id, UpdateTaskInput(task_type=canon_type, plaky_board_id=bid or None)
        )
        out["type"] = {"value": canon_type, "ok": res.get("ok")}

    if not bid:
        return out

    keys = await board_person_field_keys(bid)
    cfg = load_team_assignments()
    if keys is not None:
        eng_key = keys.get("engineer") or ""
    else:
        eng_key = (cfg.plaky_field_engineer or "").strip()
    if not eng_key:
        return out

    current_eng = await _current_person_field_value(plaky, bid, task_id, eng_key)
    if current_eng:
        out["assignee"] = {"skipped": "already_assigned"}
        return out

    pr_user = getattr(pull_request, "user", None)
    author = github_actor_payload(pr_user if isinstance(pr_user, dict) else {})
    plaky_id = await resolve_github_user_to_plaky_user_id(author)
    if not plaky_id:
        out["assignee"] = {"skipped": "no_plaky_match", "login": author.get("login")}
        return out

    # Resolve "Assigned" status from the live board (no hardcoded label).
    assigned_status_key: str | None = None
    assigned_status_val = ""
    rp = await resolve_plaky_status_patch(bid, intent="workflow_assigned")
    if rp:
        assigned_status_key, assigned_status_val = rp[0], rp[1]

    res = await update_task_internal(
        task_id,
        UpdateTaskInput(
            engineer_plaky_id=plaky_id,
            engineer_plaky_field_key=eng_key,
            status=assigned_status_val or None,
            status_plaky_field_key=assigned_status_key,
            plaky_board_id=bid,
        ),
    )
    out["assignee"] = {
        "filled": True,
        "plaky_id": plaky_id,
        "login": author.get("login"),
        "status": assigned_status_val or None,
        "ok": res.get("ok"),
    }
    return out


async def _maybe_set_needs_qa(
    plaky: PlakyClient,
    task_id: str,
    is_draft: bool,
    board_id: str = "",
) -> None:
    st = _needs_qa_status_value()
    status_field_key: str | None = None
    bid = (board_id or "").strip()
    if not st and bid:
        from boardman.plaky.dynamic_qa_status import resolve_plaky_status_patch

        resolved = await resolve_plaky_status_patch(bid, intent="workflow_needs_qa")
        if resolved:
            status_field_key, st = resolved[0], resolved[1]
    if not st:
        return
    if is_draft and settings.plaky_skip_needs_qa_for_draft:
        return
    await _update_plaky_task_status(
        task_id, st, bid, status_field_key=status_field_key
    )
    await maybe_enqueue_plaky_reorder_after_task(plaky, task_id)


async def _maybe_triage_ambiguous_pr(
    payload: PullRequestEventPayload,
    session: AsyncSession,
    top_scored: Sequence[Any] | None = None,
) -> dict[str, Any] | None:
    """
    PRs with no Fixes/Closes issue link: optional Plaky triage task + QA assignee.
    Configure under `ambiguous_pr` in team_assignments.yml. Idempotent per PR —
    reopen/edit events must not create a second triage task.
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

    prior = await session.execute(
        select(SyncLog).where(
            SyncLog.action == "pr_ambiguous_triage",
            SyncLog.github_repo == repo_name,
            SyncLog.github_ref == str(pr_number),
        )
    )
    if prior.scalars().first() is not None:
        return {
            "ok": True,
            "skipped": True,
            "message": "triage task already created for this PR",
            "ambiguous_triage": True,
        }
    title = amb.title_template.format(number=pr_number, repo=repo_name, full_name=full_name)
    description = (
        f"GitHub PR (no linked issue): {pr_url}\n\n"
        f"**Repo:** `{full_name}`\n\n"
        "This PR did not reference an issue with `Fixes #` / `Closes #` / `Resolves #`. "
        "Triage: link the right issue, add QA plan, or split work.\n"
    )
    if top_scored:
        # Surface the fuzzy pipeline's best guesses so a human can link in one click —
        # previously these only landed in the SyncLog table where nobody saw them.
        description += "\n" + format_triage_comment(top_scored) + "\n"

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
    if task_id:
        triage_comment = (
            format_pr_notice_with_url(
                headline="**PR opened (no issue link):**",
                pr_number=pr_number,
                pr_url=pr_url,
            )
            + "\n\nAutomation created this triage task because the PR did not reference an issue."
        )
        await plaky.add_comment(str(task_id), triage_comment, board_id=bid)

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

    from boardman.repos_config import get_routing_async
    routing = await get_routing_async(full_name, repo_name, settings.github_org)
    board_id = (routing.plaky_board_id if routing and routing.plaky_board_id else "") or ""

    plaky = PlakyClient()
    results = []

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
            comment = format_pr_notice_with_url(
                headline="**PR Opened:**",
                pr_number=pr_number,
                pr_url=pr_url,
            )
            await plaky.add_comment(mapping.plaky_task_id, comment, board_id=board_id or None)
            await _apply_pr_type_and_assignee(
                plaky,
                task_id=mapping.plaky_task_id,
                board_id=board_id,
                pull_request=payload.pull_request,
                repo_full=full_name,
            )
            await _maybe_set_needs_qa(plaky, mapping.plaky_task_id, is_draft, board_id)

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
        pipe_top: Sequence[Any] | None = None
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
            pipe_top = pipe.top_scored
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
                comment = format_pr_notice_with_url(
                    headline=f"**PR Opened** (automation link — {pipe.decision}):",
                    pr_number=pr_number,
                    pr_url=pr_url,
                )
                await plaky.add_comment(pipe.task_id, comment, board_id=board_id or None)
                await _apply_pr_type_and_assignee(
                    plaky,
                    task_id=pipe.task_id,
                    board_id=board_id,
                    pull_request=payload.pull_request,
                    repo_full=full_name,
                )
                await _maybe_set_needs_qa(plaky, pipe.task_id, is_draft, board_id)
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

        triage = await _maybe_triage_ambiguous_pr(payload, session, top_scored=pipe_top)
        if triage is not None:
            return triage
        return {"ok": True, "skipped": True, "message": "No linked issues found"}

    await session.commit()
    return {"ok": True, "linked": results}


async def handle_pr_edited(
    payload: PullRequestEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    """PR title/body edited: an unlinked PR gets one more shot at linking.

    A PR opened without `Fixes #N` that is later edited to include one (or given a
    clearer title) re-runs the full opened pipeline. Already-linked PRs are left
    alone — automation must not churn a link a human may have curated.
    """
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    state = (payload.pull_request.state or "").strip().casefold()
    if state and state != "open":
        return {"ok": True, "skipped": True, "message": "PR not open; edit ignored"}
    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if task_ids:
        return {"ok": True, "skipped": True, "message": "PR already linked; edit ignored"}
    return await handle_pr_opened(payload, session)


async def handle_pr_converted_to_draft(
    payload: PullRequestEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    """Ready-for-review reversed (converted_to_draft): Needs QA tasks go back to In Progress."""
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no linked Plaky tasks for this PR"}

    from boardman.repos_config import get_routing_async

    routing = await get_routing_async(payload.repository.full_name, repo_name, settings.github_org)
    bid = ((routing.plaky_board_id if routing and routing.plaky_board_id else "") or "").strip()
    if not bid:
        return {"ok": True, "skipped": True, "message": "no board id for repo"}

    from boardman.plaky.dynamic_qa_status import resolve_plaky_status_patch

    needs_qa = await resolve_plaky_status_patch(bid, intent="workflow_needs_qa")
    in_progress = await resolve_plaky_status_patch(bid, intent="workflow_in_progress")
    if not needs_qa or not in_progress:
        return {
            "ok": True,
            "skipped": True,
            "message": "needs-qa / in-progress status not resolvable from board",
        }
    nq_key, nq_id = needs_qa
    ip_key, ip_id = in_progress

    plaky = PlakyClient()
    reverted: list[dict[str, Any]] = []
    for tid in task_ids:
        current = await _current_status_value(plaky, bid, tid, nq_key)
        if not current or current != str(nq_id):
            continue
        res = await _update_plaky_task_status(tid, ip_id, bid, status_field_key=ip_key)
        session.add(
            SyncLog(
                action="pr_converted_to_draft",
                github_repo=repo_name,
                github_ref=str(pr_number),
                plaky_task_id=tid,
                detail=json.dumps({"from": "needs_qa", "to_status": ip_id}, default=str),
            )
        )
        reverted.append({"task_id": tid, "plaky": res})

    await session.commit()
    if reverted:
        await maybe_enqueue_plaky_reorder_after_task(plaky, reverted[0]["task_id"])
    return {"ok": True, "updated": reverted, "event": "converted_to_draft"}


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

    from boardman.repos_config import get_routing_async

    routing = await get_routing_async(payload.repository.full_name, repo_name, settings.github_org)
    board_id = (routing.plaky_board_id if routing and routing.plaky_board_id else "") or ""

    for tid in task_ids:
        await _maybe_set_needs_qa(plaky, tid, is_draft=False, board_id=board_id or "")

    await session.commit()
    return {"ok": True, "tasks": task_ids, "event": "ready_for_review"}


async def handle_pr_review_requested(
    payload: PullRequestEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no linked Plaky tasks for this PR"}

    from boardman.repos_config import get_routing_async

    routing = await get_routing_async(payload.repository.full_name, repo_name, settings.github_org)
    board_id = (routing.plaky_board_id if routing and routing.plaky_board_id else "") or ""

    in_qa = (settings.plaky_pr_in_qa_status or settings.plaky_status_in_qa or "").strip()
    in_qa_field_key: str | None = None
    bid = (board_id or "").strip()
    if not in_qa and bid:
        from boardman.plaky.dynamic_qa_status import resolve_plaky_status_patch

        rp = await resolve_plaky_status_patch(bid, intent="workflow_in_qa")
        if rp:
            in_qa_field_key, in_qa = rp[0], rp[1]
    if not in_qa:
        return {"ok": True, "skipped": True, "message": "in_qa status not configured or discoverable"}

    plaky = PlakyClient()
    for tid in task_ids:
        await _update_plaky_task_status(
            tid, in_qa, board_id or "", status_field_key=in_qa_field_key
        )
    await session.commit()
    if task_ids:
        await maybe_enqueue_plaky_reorder_after_task(plaky, task_ids[0])
    return {"ok": True, "tasks": task_ids, "status": in_qa, "event": "review_requested"}


async def handle_pr_synchronized(
    payload: PullRequestEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    """New commits pushed (pull_request.synchronize): if a linked task is currently QA-rejected,
    the developer has resumed work → move it back to In Progress.
    """
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no linked Plaky tasks for this PR"}

    from boardman.repos_config import get_routing_async

    routing = await get_routing_async(payload.repository.full_name, repo_name, settings.github_org)
    board_id = (routing.plaky_board_id if routing and routing.plaky_board_id else "") or ""
    bid = board_id.strip()
    if not bid:
        return {"ok": True, "skipped": True, "message": "no board id for repo"}

    from boardman.plaky.dynamic_qa_status import resolve_plaky_status_patch

    rejected = await resolve_plaky_status_patch(bid, intent="github_pr_review_changes_requested")
    in_progress = await resolve_plaky_status_patch(bid, intent="workflow_in_progress")
    if not rejected or not in_progress:
        return {
            "ok": True,
            "skipped": True,
            "message": "qa-rejected / in-progress status not resolvable from board",
        }
    rej_key, rej_id = rejected
    ip_key, ip_id = in_progress

    plaky = PlakyClient()
    resumed: list[dict[str, Any]] = []
    for tid in task_ids:
        current = await _current_status_value(plaky, bid, tid, rej_key)
        if not current or current != str(rej_id):
            continue
        res = await _update_plaky_task_status(tid, ip_id, bid, status_field_key=ip_key)
        session.add(
            SyncLog(
                action="pr_resumed_in_progress",
                github_repo=repo_name,
                github_ref=str(pr_number),
                plaky_task_id=tid,
                detail=json.dumps({"from": "qa_rejected", "to_status": ip_id}, default=str),
            )
        )
        resumed.append({"task_id": tid, "plaky": res})

    await session.commit()
    if resumed:
        await maybe_enqueue_plaky_reorder_after_task(plaky, resumed[0]["task_id"])
    return {"ok": True, "updated": resumed, "event": "resumed_after_rejection"}


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
    for issue_num in linked_issues:
        mapping = await find_plaky_task_by_issue(repo_name, issue_num, session)
        if mapping:
            affected_tasks.add(mapping.plaky_task_id)

    if not affected_tasks:
        await remove_pr_row(payload.pull_request, payload.repository, session)
        await session.commit()
        return {"ok": True, "skipped": True, "message": "No linked Plaky tasks for this PR"}

    from boardman.repos_config import get_routing_async

    merge_routing = await get_routing_async(payload.repository.full_name, repo_name, settings.github_org)
    board_id_merge = (merge_routing.plaky_board_id if merge_routing and merge_routing.plaky_board_id else "") or ""

    # Merged → Completed. Explicit env wins; otherwise resolve "Completed" from the live board.
    merge_status = (settings.plaky_pr_merge_status or "").strip()
    merge_status_field_key: Optional[str] = None
    if not merge_status and board_id_merge:
        from boardman.plaky.dynamic_qa_status import resolve_plaky_status_patch

        rp = await resolve_plaky_status_patch(board_id_merge, intent="workflow_completed")
        if rp:
            merge_status_field_key, merge_status = rp[0], rp[1]
    if not merge_status:
        merge_status = (settings.plaky_status_completed or "completed").strip()

    results: list[dict[str, Any]] = []

    for task_id in sorted(affected_tasks):
        if settings.plaky_complete_when_all_prs_merged and await has_any_open_pr_for_task(
            session, plaky_task_id=task_id
        ):
            results.append(
                {
                    "task_id": task_id,
                    "deferred": True,
                    "reason": "other_prs_still_open_or_active",
                }
            )
            continue

        await _update_plaky_task_status(
            task_id, merge_status, board_id_merge, status_field_key=merge_status_field_key
        )
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
        await maybe_enqueue_plaky_reorder_after_task(plaky, task_id)

    await remove_pr_row(payload.pull_request, payload.repository, session)

    await session.commit()
    return {"ok": True, "updated": results}


async def handle_pr_review_comment(payload: PullRequestReviewCommentEventPayload, session: AsyncSession) -> dict:
    """Handle PR review comment events - mark as In QA if commenter is assigned QA."""
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number if payload.pull_request else 0
    pr_url = payload.pull_request.html_url if payload.pull_request else ""
    full_name = payload.repository.full_name if payload.repository else ""

    comment = payload.comment
    if not isinstance(comment, dict):
        return {"ok": True, "skipped": True, "message": "no comment payload"}
    commenter = comment.get("user")
    commenter_login = commenter.get("login") if isinstance(commenter, dict) else None

    if not commenter_login:
        return {"ok": False, "message": "No commenter login found"}

    linked_issues = await get_linked_issue_numbers(payload.pull_request.body if payload.pull_request else None)

    task_ids_with_issue: list[tuple[str, Optional[int]]] = []
    for issue_num in linked_issues:
        mapping = await find_plaky_task_by_issue(repo_name, issue_num, session)
        if mapping:
            task_ids_with_issue.append((mapping.plaky_task_id, int(issue_num)))

    if not task_ids_with_issue:
        from boardman.services.pr_task_registry import distinct_task_ids_for_pr

        for tid in await distinct_task_ids_for_pr(session, github_repo=repo_name, github_pr_number=pr_number):
            task_ids_with_issue.append((tid, None))

    if not task_ids_with_issue:
        return {"ok": True, "skipped": True, "message": "No linked Plaky tasks for this PR"}

    from boardman.plaky.dynamic_qa_status import (
        github_actor_payload,
        resolve_github_user_to_plaky_user_id,
        resolve_plaky_status_patch,
        resolve_qa_assignee_field_key,
    )
    from boardman.repos_config import get_routing_async

    cfg = load_team_assignments()
    routing = await get_routing_async(full_name, repo_name, settings.github_org)
    board_id = (routing.plaky_board_id if routing and routing.plaky_board_id else "") or ""

    qa_field = await resolve_qa_assignee_field_key(board_id, cfg.plaky_field_qa)
    if not qa_field:
        return {"ok": True, "skipped": True, "message": "QA field not configured or discoverable on board"}

    plaky = PlakyClient()
    results = []

    reviewer_plaky_id: str | None = None
    for m in cfg.members:
        gl = (m.github_login or "").strip()
        if gl and gl.casefold() == commenter_login.casefold():
            reviewer_plaky_id = m.id
            break
    if not reviewer_plaky_id and commenter_login:
        commenter_dict = commenter if isinstance(commenter, dict) else {}
        reviewer_plaky_id = await resolve_github_user_to_plaky_user_id(
            github_actor_payload(commenter_dict)
        )

    for task_id, issue_num in task_ids_with_issue:
        task_info = await plaky.get_board_item_public(board_id, task_id)
        if not task_info.get("ok") or not task_info.get("item"):
            continue

        item = task_info.get("item", {})
        _qa_ids = plaky_item_person_ids(item, qa_field)
        assigned_qa_id = _qa_ids[0] if _qa_ids else ""

        if assigned_qa_id and reviewer_plaky_id and assigned_qa_id == reviewer_plaky_id:
            status_field_key: str | None = None
            status_to_set = (settings.plaky_pr_in_qa_status or settings.plaky_status_in_qa or "").strip()
            if not status_to_set and board_id:
                rp = await resolve_plaky_status_patch(board_id, intent="workflow_in_qa")
                if rp:
                    status_field_key, status_to_set = rp[0], rp[1]
            if not status_to_set:
                continue
            await _update_plaky_task_status(
                task_id, status_to_set, board_id, status_field_key=status_field_key
            )
            comment_text = f"**PR Comment:** by @{commenter_login}"
            await plaky.add_comment(task_id, comment_text, board_id=board_id or None)

            log = SyncLog(
                action="in_qa_comment",
                github_repo=repo_name,
                github_ref=str(pr_number),
                plaky_task_id=task_id,
                detail=json.dumps(
                    {
                        "issue_number": issue_num,
                        "pr_url": pr_url,
                        "commenter": commenter_login,
                        "status": status_to_set,
                    },
                    default=str,
                ),
            )
            session.add(log)
            results.append(
                {
                    "issue": issue_num,
                    "task_id": task_id,
                    "action": "in_qa_comment",
                    "status": status_to_set,
                }
            )

    await session.commit()
    if results:
        tid0 = results[0].get("task_id") if results else None
        if tid0:
            await maybe_enqueue_plaky_reorder_after_task(plaky, str(tid0))
    return {"ok": True, "updated": results}
