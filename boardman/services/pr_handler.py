import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from boardman.assignment.config import load_team_assignments
from boardman.assignment.qa_picker import pick_qa_for_repo
from boardman.database.models import SyncLog
from boardman.github.webhooks import PullRequestEventPayload
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
from boardman.settings import settings


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
        qid, _ = pick_qa_for_repo(full_name)
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

    linked_issues = await get_linked_issue_numbers(payload.pull_request.body)

    plaky = PlakyClient()
    results = []

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
            affected_tasks.add(mapping.plaky_task_id)
            merge_detail = {
                "issue_number": issue_num,
                "pr_url": pr_url,
                "awaiting_all_prs": bool(settings.plaky_complete_when_all_prs_merged),
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
                    "task_id": task_id,
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

    await session.commit()
    return {"ok": True, "updated": results}
