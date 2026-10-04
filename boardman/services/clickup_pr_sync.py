"""GitHub pull-request webhooks to ClickUp tasks. The ClickUp counterpart of the Plaky paths in
``pr_handler``; ``pr_handler`` dispatches here when ``TASK_PROVIDER=clickup``.

A PR is attached to the task its issue already owns (a closing keyword, a title reference or an
``issue-N`` branch). The rules the Plaky path enforces carry over, translated to ClickUp:

* A PR that is linked LATE or replayed never moves a task backwards, and never stages review work
  for a task that is already finished. A fresh PR asks for QA even on a task past that point.
* The developer is fill-only and must be an eligible developer; QA is never the PR author.
* QA is assigned when the PR opens (not at task creation) and is never overwritten.
* Merging completes a task only for a closing keyword in the PR description, and only when no
  other PR for that task is still open.
* A task parked in the review queue with no open PR left goes back to in progress.

Not ported yet: the fuzzy "no issue named" matching pipeline and orphan triage. A PR that names no
issue with a ClickUp task is reported as skipped. See docs/CLICKUP.md.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.assignment.config import load_team_assignments
from boardman.clickup.client import ClickUpClient
from boardman.clickup.statuses import status_for_intent
from boardman.clickup.task_view import (
    current_assignees,
    current_status,
    sync_type_tag,
    user_ids,
)
from boardman.database.models import (
    IssueTaskMap,
    PrTaskLifecycle,
    PullRequestTaskLink,
    SyncLog,
)
from boardman.github.pr_exclusion import pr_sync_exclusion_reason
from boardman.github.webhooks import (
    DeploymentStatusEventPayload,
    PullRequestEventPayload,
    PullRequestReviewCommentEventPayload,
)
from boardman.services.clickup_task_ops import (
    intent_of,
    qa_from_field,
    read_task,
    set_intent_status,
    stamp,
    stamped_qa,
)
from boardman.services.comment_dedupe import (
    comment_already_synced,
    edit_changed_the_text,
    github_activity_marker,
    mirror_github_activity,
)
from boardman.services.issue_handler import (
    explicit_issue_numbers,
    find_plaky_task_by_issue,
    get_linked_issue_numbers,
    linked_issue_numbers_for_pr,
)
from boardman.services.pr_link_comment import format_pr_notice_with_url
from boardman.services.pr_sync_common import (
    COMMITS_SINCE_REVIEW_ESCALATION_THRESHOLD,
    QA_VERDICT_INTENTS,
    ensure_links_live,
    issue_link_source,
    member_by_name,
    prs_for_commit_sha,
)
from boardman.services.pr_task_linking import (
    format_triage_comment,
    run_pr_task_pipeline_clickup,
    should_run_pipeline,
)
from boardman.services.pr_task_registry import (
    _PR_OWNED_LINK_SOURCES,
    _PR_TASK_CREATED_LINK_SOURCE,
    _PR_TASK_PENDING_LINK_SOURCE,
    _SUPERSEDED_LINK_SOURCE,
    _WEAK_COMPLETION_LINK_SOURCES,
    distinct_task_ids_for_pr,
    has_any_open_pr_for_task,
    mark_pr_merged,
    mark_pr_withdrawn,
    upsert_pr_task_link,
)
from boardman.services.pr_tracker import remove_pr_row, upsert_pr_row
from boardman.services.sync_state import (
    resolve_pr_state,
    status_intent_would_regress,
)
from boardman.settings import settings

_log = logging.getLogger(__name__)


# -- small building blocks ---------------------------------------------------------------------


# -- type, developer, QA, needs-QA ---------------------------------------------------------------


async def _apply_type_and_assignee(
    c: ClickUpClient,
    *,
    task_id: str,
    pull_request: Any,
    repo_full: str,
    allow_regression: bool = True,
) -> dict[str, Any]:
    """Set the type from the PR, and fill the developer (and "assigned") when nobody owns the task."""
    from boardman.assignment.developer_eligibility import filter_developer
    from boardman.assignment.github_user_resolution import (
        github_actor_payload,
        resolve_github_user_to_user_id,
    )
    from boardman.github.pr_signals import infer_task_type_from_pr, pr_label_names

    out: dict[str, Any] = {}
    task = await read_task(c, task_id)
    if task is None:
        return {"skipped": "task unreadable"}

    head = getattr(pull_request, "head", None)
    head_ref = str(head.get("ref")) if isinstance(head, dict) else ""
    canon_type = infer_task_type_from_pr(
        head_ref,
        pr_label_names(getattr(pull_request, "labels", None)),
        title=str(getattr(pull_request, "title", "") or ""),
        body=str(getattr(pull_request, "body", "") or ""),
    )
    pr_state = resolve_pr_state(
        pull_request, repo_full_name=repo_full, repo_name=repo_full.rsplit("/", 1)[-1]
    )
    if canon_type:
        ops = await sync_type_tag(c, task_id, task, canon_type)
        out["type"] = {"value": canon_type, "ok": all(bool(o.get("ok")) for o in ops)}
        # A priority set on the ISSUE is the human's call; a PR with no priority label has no
        # opinion and must not reset it.
        if pr_state.priority_explicit:
            await c.update_task_fields(task_id, priority=pr_state.priority)

    if current_assignees(task):
        out["assignee"] = {"skipped": "already_assigned"}
        return out

    pr_user = getattr(pull_request, "user", None)
    author = github_actor_payload(pr_user if isinstance(pr_user, dict) else {})
    dev_id = str(await resolve_github_user_to_user_id(author) or "").strip()
    if not dev_id:
        out["assignee"] = {"skipped": "no_clickup_match", "login": author.get("login")}
        return out
    dev_id, refusal = filter_developer(dev_id)
    if not dev_id:
        out["assignee"] = {
            "skipped": "not_a_developer",
            "login": author.get("login"),
            "reason": refusal,
        }
        return out

    status_name = status_for_intent("workflow_assigned")
    if status_name and not allow_regression:
        if status_intent_would_regress(intent_of(task), "workflow_assigned"):
            status_name = ""
    res = await c.update_task_fields(
        task_id, add_assignee_ids=user_ids(dev_id), status=status_name or None
    )
    out["assignee"] = {
        "filled": bool(res.get("ok")),
        "clickup_id": dev_id,
        "login": author.get("login"),
        "status": status_name or None,
        "ok": res.get("ok"),
    }
    return out


async def _assign_qa_for_pr(
    c: ClickUpClient,
    *,
    task_id: str,
    repo_full: str,
    pr_number: int,
    pr_author_login: str = "",
    task_url: str = "",
    session: AsyncSession,
) -> dict[str, Any]:
    """Assign QA when a PR opens: pick, record it, mention them on the PR and request a review.

    Never overwrites an already-assigned QA. "Already assigned" is read from the QA users field
    when one is configured, and from the QA recorded on the PR's link row otherwise.
    """
    from boardman.assignment.qa_picker import pick_qa_for_repo
    from boardman.github.pr_actions import (
        comment_on_pr,
        has_qa_assignment_comment,
        request_reviewers,
    )
    from boardman.services.pr_task_registry import active_pr_counts_by_qa, stamp_qa_on_pr_links

    out: dict[str, Any] = {}
    cfg = load_team_assignments()
    repo_short = repo_full.rsplit("/", 1)[-1]
    task = await read_task(c, task_id) or {}

    qid = qa_from_field(task) or await stamped_qa(session, repo_short, pr_number, task_id)
    already = bool(qid)
    why = "already assigned" if already else ""

    if not qid:
        # Bug-typed tasks go to the QA bug specialist, unless she authored the PR.
        specialist = (getattr(cfg, "qa_bug_specialist", "") or "").strip()
        is_bug = any(
            str(t.get("name") or "").casefold() == "type:bug"
            for t in task.get("tags") or []
            if isinstance(t, dict)
        )
        if specialist and is_bug:
            sm = member_by_name(cfg, specialist)
            if sm is None:
                _log.warning("qa_bug_specialist %r not in roster or fallback", specialist)
            elif (getattr(sm, "github_login", "") or "").casefold() == (
                pr_author_login or ""
            ).casefold() and pr_author_login:
                _log.info("qa_bug_specialist authored PR #%s - using ranked pick", pr_number)
            else:
                qid = str(sm.id)
                why = f"bug task -> QA bug specialist {getattr(sm, 'display', specialist)}"
    if not qid:
        try:
            workload = await active_pr_counts_by_qa(session)
        except Exception as exc:  # noqa: BLE001 - the cap is a nicety, picking must still work
            _log.warning("qa workload count failed, picking without cap: %s", exc)
            workload = None
        qid, why = await pick_qa_for_repo(
            repo_full, exclude_login=pr_author_login, qa_workload=workload
        )
    if not qid:
        return {"skipped": "no eligible QA", "reason": why}

    member = next((m for m in cfg.members if (m.id or "").strip() == str(qid)), None)
    qa_login = (getattr(member, "github_login", "") or "").strip() if member else ""
    qa_display = (getattr(member, "display", "") or "").strip() if member else ""

    if pr_author_login and qa_login:
        try:
            from boardman.services.pr_review_nudges import ensure_tracked

            await ensure_tracked(
                session,
                github_repo=repo_short,
                github_pr_number=pr_number,
                developer_login=pr_author_login,
                primary_qa_login=qa_login,
            )
        except Exception as exc:  # noqa: BLE001 - the nudge sweep is a bonus feature
            _log.warning("pr_review_nudges.ensure_tracked failed for PR #%s: %s", pr_number, exc)

    if already:
        out["clickup_qa"] = {
            "id": str(qid),
            "display": qa_display,
            "skipped": "qa_already_assigned",
        }
        # A previous GitHub notification may have failed independently of the write; do not
        # re-notify when it is already there.
        if await has_qa_assignment_comment(repo_full, pr_number):
            out["github_comment"] = {"ok": True, "skipped": "already_commented"}
            return out
    else:
        res = await c.assign_qa(task_id, str(qid))
        out["clickup_qa"] = {
            "id": str(qid),
            "display": qa_display,
            "ok": res.get("ok"),
            "via": res.get("via"),
            "reason": why[:220],
        }
        try:
            await stamp_qa_on_pr_links(
                session, github_repo=repo_short, github_pr_number=pr_number, qa_plaky_id=str(qid)
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("stamp_qa_on_pr_links failed for PR #%s: %s", pr_number, exc)

    mention = f"@{qa_login}" if qa_login else (qa_display or "QA")
    task_ref = f"[ClickUp task]({task_url})" if task_url else f"ClickUp task {task_id}"
    body = (
        f"{mention} you've been assigned as **QA reviewer** for this PR by Boardman.\n\n"
        f"Linked task: {task_ref}\n\n{why[:500]}"
    )
    out["github_comment"] = await comment_on_pr(repo_full, pr_number, body)
    if qa_login and qa_login.casefold() != (pr_author_login or "").casefold():
        out["github_reviewer"] = await request_reviewers(repo_full, pr_number, [qa_login])
    return out


async def _maybe_set_needs_qa(
    c: ClickUpClient, task_id: str, is_draft: bool, *, allow_regression: bool = True
) -> dict[str, Any]:
    if is_draft and settings.skip_needs_qa_for_draft:
        return {"skipped": "draft"}
    return await set_intent_status(
        c, task_id, "workflow_needs_qa", guard=None if allow_regression else "backwards"
    )


# -- linking ---------------------------------------------------------------------------------------


async def link_pr_to_issue_task(
    session: AsyncSession,
    c: ClickUpClient,
    *,
    payload: PullRequestEventPayload,
    issue_number: int,
    mapping: Any,
    is_draft: bool,
    headline: str,
    is_late_link: bool = False,
    announce: bool = True,
    link_source: str = "issue_keyword",
    skip_qa_if_finished: bool = False,
) -> bool:
    """Attach one PR to the task an issue already owns and run the PR workflow.

    Returns whether the PR is now linked. False means the link was retired when the PR re-pointed
    at another issue; the caller must not count it. Every step is idempotent, so a replayed edit
    re-runs it without duplicating.
    """
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    pr_url = payload.pull_request.html_url
    task_id = str(mapping.plaky_task_id)

    link_row = await upsert_pr_task_link(
        session,
        github_repo=repo_name,
        github_pr_number=pr_number,
        plaky_task_id=task_id,
        github_issue_number=int(issue_number),
        link_source=link_source,
    )
    if str(getattr(link_row, "link_source", "") or "") == _SUPERSEDED_LINK_SOURCE:
        _log.info("PR #%s: the link to issue #%s is superseded", pr_number, issue_number)
        return False
    if announce:
        await mirror_github_activity(
            session,
            c,
            task_id=task_id,
            action="pr_link_notice",
            marker=(
                f"github:pr-link-notice:{repo_name}:{pr_number}:{issue_number}"
                f"{'' if headline == '**PR Opened:**' else ':' + headline.strip('*: ')}"
            ),
            body=format_pr_notice_with_url(headline=headline, pr_number=pr_number, pr_url=pr_url),
            github_repo=repo_name,
            github_ref=str(pr_number),
        )
    await _apply_type_and_assignee(
        c,
        task_id=task_id,
        pull_request=payload.pull_request,
        repo_full=payload.repository.full_name,
        allow_regression=not is_late_link,
    )
    if skip_qa_if_finished:
        task = await read_task(c, task_id)
        if intent_of(task) == "workflow_completed":
            stamp(
                session,
                "pr_linked",
                repo_name,
                pr_number,
                task_id,
                issue_number=issue_number,
                pr_url=pr_url,
                qa_skipped="workflow_completed",
            )
            return True

    pr_user = payload.pull_request.user or {}
    qa = await _assign_qa_for_pr(
        c,
        task_id=task_id,
        repo_full=payload.repository.full_name,
        pr_number=pr_number,
        pr_author_login=str(pr_user.get("login") or "") if isinstance(pr_user, dict) else "",
        task_url=mapping.plaky_task_url or "",
        session=session,
    )
    _log.info("PR #%s QA assignment: %s", pr_number, {k: qa[k] for k in list(qa)[:3]})
    await _maybe_set_needs_qa(c, task_id, is_draft, allow_regression=not is_late_link)
    stamp(
        session,
        "pr_linked",
        repo_name,
        pr_number,
        task_id,
        issue_number=issue_number,
        pr_url=pr_url,
    )
    return True


async def retire_superseded_task(
    session: AsyncSession,
    c: ClickUpClient,
    *,
    task_id: str,
    repo_name: str,
    pr_number: int,
    canonical_task_ids: str,
    pr_owned: bool = True,
) -> None:
    """Tell a superseded card it is superseded, and stop it asking QA for review.

    It is not closed or deleted: it may carry comments and a person's edits. A card the PR did not
    open is somebody else's, so it only says so and changes nothing else.
    """
    note = (
        (
            f"Superseded: PR #{pr_number} now says which issue it closes, so its work is "
            f"tracked on task {canonical_task_ids}. This card was created for the PR before that "
            "was known and will not receive further updates."
        )
        if pr_owned
        else (
            f"Unlinked: PR #{pr_number} now says which issue it closes, so its updates go to "
            f"task {canonical_task_ids} from here. This card was matched to the PR "
            "automatically; its own status is unchanged."
        )
    )
    await mirror_github_activity(
        session,
        c,
        task_id=task_id,
        action="pr_task_superseded",
        marker=f"github:pr-task-superseded:{repo_name}:{pr_number}:{task_id}",
        body=note,
        github_repo=repo_name,
        github_ref=str(pr_number),
    )
    if not pr_owned:
        return
    task = await read_task(c, task_id)
    if intent_of(task) == "workflow_needs_qa":
        await set_intent_status(c, task_id, "workflow_in_progress", task=task)


# -- opened --------------------------------------------------------------------------------------


async def handle_pr_opened(
    payload: PullRequestEventPayload,
    session: AsyncSession,
    *,
    is_replay: bool = False,
    client: ClickUpClient | None = None,
) -> dict[str, Any]:
    c = client or ClickUpClient()
    repo_name = payload.repository.name
    is_reopen = str(getattr(payload, "action", "") or "").casefold() == "reopened"
    # A reopen or a replay is not a PR arriving for the first time: fill in what is missing and
    # move nothing backwards.
    is_rerun = is_replay or is_reopen
    await ensure_links_live(payload, session)
    pr_number = payload.pull_request.number
    is_draft = bool(payload.pull_request.draft)
    full_name = payload.repository.full_name

    opened_state = resolve_pr_state(
        payload.pull_request, repo_full_name=full_name, repo_name=repo_name
    )
    base = getattr(payload.pull_request, "base", None)
    base_ref = str(base.get("ref") or "") if isinstance(base, dict) else ""
    pr_user = payload.pull_request.user if isinstance(payload.pull_request.user, dict) else None
    exclusion = pr_sync_exclusion_reason(
        base_ref=base_ref, head_ref=opened_state.head_ref, pr_user=pr_user
    )
    if exclusion:
        return {"ok": True, "skipped": True, "excluded": True, "message": exclusion}

    linked = linked_issue_numbers_for_pr(
        body=payload.pull_request.body,
        title=payload.pull_request.title,
        head_ref=opened_state.head_ref,
        repo_full_name=full_name,
    )
    written = explicit_issue_numbers(
        payload.pull_request.body, payload.pull_request.title, repo_full_name=full_name
    )
    body_written = explicit_issue_numbers(payload.pull_request.body, repo_full_name=full_name)

    if not is_draft:
        await upsert_pr_row(payload.pull_request, payload.repository, session)

    results: list[dict[str, Any]] = []
    for issue_num in linked:
        mapping = await find_plaky_task_by_issue(repo_name, issue_num, session)
        if not mapping:
            continue
        attached = await link_pr_to_issue_task(
            session,
            c,
            payload=payload,
            issue_number=int(issue_num),
            mapping=mapping,
            is_draft=is_draft,
            headline="**PR Reopened:**" if is_reopen else "**PR Opened:**",
            is_late_link=is_rerun,
            link_source=issue_link_source(int(issue_num), body_written, written),
        )
        if attached:
            results.append({"issue": issue_num, "task_id": mapping.plaky_task_id})

    live = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if not results and not live:
        # No issue with a task: try to match an existing task, else make one for the PR.
        return await _link_or_create_for_orphan_pr(
            payload,
            session,
            c,
            opened_state=opened_state,
            linked=linked,
            written=written,
            is_draft=is_draft,
            is_reopen=is_reopen,
            is_rerun=is_rerun,
        )
    await session.commit()
    return {"ok": True, "linked": results}


async def _link_or_create_for_orphan_pr(
    payload: PullRequestEventPayload,
    session: AsyncSession,
    c: ClickUpClient,
    *,
    opened_state: Any,
    linked: list[int],
    written: list[int],
    is_draft: bool,
    is_reopen: bool,
    is_rerun: bool,
) -> dict[str, Any]:
    """A PR with no issue that has a task: fuzzy-match an existing task, else (when enabled) create one."""
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    pr_url = payload.pull_request.html_url
    full_name = payload.repository.full_name
    pipe_top = None
    run = settings.pr_linking_pipeline_enabled and await should_run_pipeline(
        payload.pull_request.body,
        repo_full_name=full_name,
        pr_title=payload.pull_request.title,
        head_ref=opened_state.head_ref,
    )
    if run:
        pr_user = payload.pull_request.user if isinstance(payload.pull_request.user, dict) else {}
        author = pr_user.get("login")
        pipe = await run_pr_task_pipeline_clickup(
            session=session,
            client=c,
            repo_full=full_name,
            repo_name=repo_name,
            org=settings.github_org,
            pr_number=pr_number,
            pr_title=payload.pull_request.title,
            pr_body=payload.pull_request.body,
            head=payload.pull_request.head,
            pr_author_login=author,
            pr_author_email=pr_user.get("email"),
            pr_author_name=pr_user.get("name"),
        )
        pipe_top = pipe.top_scored
        stamp(
            session,
            "pr_link_pipeline",
            repo_name,
            pr_number,
            pipe.task_id or "",
            decision=pipe.decision,
            score=pipe.score,
            reason=pipe.reason,
            detail=pipe.log_detail,
            triage_comment=format_triage_comment(pipe.top_scored)
            if pipe.decision == "triage"
            else None,
        )
        if pipe.decision in ("auto_link", "llm_link") and pipe.task_id:
            row = await upsert_pr_task_link(
                session,
                github_repo=repo_name,
                github_pr_number=pr_number,
                plaky_task_id=pipe.task_id,
                github_issue_number=0,
                link_source=pipe.decision,
            )
            if str(getattr(row, "link_source", "") or "") == _SUPERSEDED_LINK_SOURCE:
                # Retired when the PR named an issue; a fuzzy match is not the author saying otherwise.
                await session.commit()
                return {
                    "ok": True,
                    "skipped": True,
                    "message": "the matched card was superseded by an issue link",
                }
            await c.add_comment(
                pipe.task_id,
                format_pr_notice_with_url(
                    headline=(
                        f"**PR {'Reopened' if is_reopen else 'Opened'}** "
                        f"(automation link, {pipe.decision}):"
                    ),
                    pr_number=pr_number,
                    pr_url=pr_url,
                ),
            )
            await _apply_type_and_assignee(
                c,
                task_id=pipe.task_id,
                pull_request=payload.pull_request,
                repo_full=full_name,
                allow_regression=not is_rerun,
            )
            await _assign_qa_for_pr(
                c,
                task_id=pipe.task_id,
                repo_full=full_name,
                pr_number=pr_number,
                pr_author_login=str(author or ""),
                session=session,
            )
            await _maybe_set_needs_qa(c, pipe.task_id, is_draft, allow_regression=not is_rerun)
            stamp(
                session,
                "pr_linked_fuzzy",
                repo_name,
                pr_number,
                pipe.task_id,
                pr_url=pr_url,
                pipeline=pipe.decision,
                score=pipe.score,
            )
            await session.commit()
            return {
                "ok": True,
                "linked": [{"task_id": pipe.task_id, "via": pipe.decision}],
                "pipeline": pipe.decision,
            }
        await session.commit()

    triage = await maybe_triage_ambiguous_pr(
        payload,
        session,
        c,
        top_scored=pipe_top,
        # Written references only: claiming an issue for this card binds later edits and closes of
        # that issue to it, and a branch name is too weak a signal for that.
        orphan_issue_number=int(written[0]) if written else 0,
    )
    if triage is not None:
        return triage
    await session.commit()
    message = (
        f"named issue(s) {sorted(linked)} but none has a ClickUp task"
        if linked
        else "No linked issues found and no existing task matched"
    )
    return {"ok": True, "skipped": True, "message": message}


async def maybe_triage_ambiguous_pr(
    payload: PullRequestEventPayload,
    session: AsyncSession,
    c: ClickUpClient,
    *,
    top_scored: Any = None,
    orphan_issue_number: int = 0,
) -> dict[str, Any] | None:
    """A PR that matches no existing task gets a REAL task, not a stub: titled after the PR, typed
    from its branch and labels, owned by the PR author, "needs QA" when it is ready for review,
    linked, and given a QA. Only when ``ambiguous_pr.enabled``; idempotent per PR."""
    from boardman.assignment.developer_eligibility import filter_developer
    from boardman.assignment.github_user_resolution import (
        github_actor_payload,
        resolve_github_user_to_user_id,
    )
    from boardman.github.pr_signals import infer_task_type_from_pr, pr_label_names

    cfg = load_team_assignments()
    amb = cfg.ambiguous_pr
    if not amb.enabled:
        return None
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    pr_url = payload.pull_request.html_url
    full_name = payload.repository.full_name

    # Never manufacture review work for a PR that already shipped (a replay, a redelivery).
    pr_state = str(getattr(payload.pull_request, "state", "open") or "open").casefold()
    if pr_state != "open" or bool(getattr(payload.pull_request, "merged", False)):
        return {
            "ok": True,
            "skipped": True,
            "message": f"PR #{pr_number} is already {pr_state}; not creating a task for finished work",
            "ambiguous_triage": True,
        }

    from boardman.repos_config import get_routing_async

    routing = await get_routing_async(full_name, repo_name, settings.github_org)
    list_id = (
        (settings.clickup_triage_list_id or "").strip()
        or ((getattr(routing, "clickup_list_id", "") or "").strip() if routing else "")
        or (settings.clickup_default_list_id or "").strip()
    )
    if not list_id:
        return {
            "ok": True,
            "skipped": True,
            "message": "no ClickUp list resolvable for the orphan-PR task",
        }

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
            "message": "task already created for this PR",
            "ambiguous_triage": True,
        }
    reservation = PullRequestTaskLink(
        github_repo=repo_name,
        github_pr_number=pr_number,
        plaky_task_id=f"pending:{uuid.uuid4().hex}",
        github_issue_number=0,
        link_source=_PR_TASK_PENDING_LINK_SOURCE,
    )
    try:
        async with session.begin_nested():
            session.add(reservation)
            await session.flush()
    except IntegrityError:
        if await distinct_task_ids_for_pr(
            session, github_repo=repo_name, github_pr_number=pr_number
        ):
            return {
                "ok": True,
                "skipped": True,
                "message": "task already created for this PR",
                "ambiguous_triage": True,
            }
        raise

    pr = payload.pull_request
    head = getattr(pr, "head", None)
    head_ref = str(head.get("ref") or "") if isinstance(head, dict) else ""
    labels = pr_label_names(getattr(pr, "labels", None))
    task_type = (
        infer_task_type_from_pr(
            head_ref,
            labels,
            title=str(getattr(pr, "title", "") or ""),
            body=str(getattr(pr, "body", "") or ""),
        )
        or "Feature"
    )
    is_draft = bool(getattr(pr, "draft", False))
    pr_user = getattr(pr, "user", None)
    author_login = str(pr_user.get("login") or "").strip() if isinstance(pr_user, dict) else ""
    author_id = ""
    if isinstance(pr_user, dict):
        author_id = str(await resolve_github_user_to_user_id(github_actor_payload(pr_user)) or "")
    author_id, _refusal = filter_developer(author_id)

    title = str(getattr(pr, "title", "") or "").strip() or amb.title_template.format(
        number=pr_number, repo=repo_name, full_name=full_name
    )
    description = (
        f"Auto-created from GitHub PR (no existing task matched): {pr_url}\n\n"
        f"Repo: {full_name}  Branch: {head_ref or '?'}  Author: {author_login or 'unknown'}\n\n"
        "The PR did not reference an issue and fuzzy matching found no confident task, "
        "so this task now represents that work.\n"
    )
    if top_scored:
        description += "\nClosest existing candidates considered:\n" + format_triage_comment(
            top_scored
        )
    state = resolve_pr_state(pr, repo_full_name=full_name, repo_name=repo_name)
    # A non-draft PR is up for review now; a draft's status follows its owner.
    status = (status_for_intent("workflow_needs_qa") if not is_draft else "") or status_for_intent(
        "workflow_assigned" if author_id else "workflow_needs_assigned"
    )
    res = await c.create_task(
        title,
        description,
        state.priority,
        board_id=list_id,
        status=status or None,
        assignee_ids=user_ids(author_id),
        tags=[repo_name.lower(), f"type:{task_type.strip().lower()}"],
    )
    if not res.get("ok"):
        await session.delete(reservation)
        return {"ok": False, "message": res.get("message"), "ambiguous_triage": True}

    task_id = str(res.get("task_id") or "")
    if not task_id:
        await session.delete(reservation)
        qa_res: dict[str, Any] = {"skipped": "task id missing from create result"}
    else:
        reservation.plaky_task_id = task_id
        reservation.link_source = _PR_TASK_CREATED_LINK_SOURCE
        if settings.pr_task_cleanup_enabled:
            session.add(
                PrTaskLifecycle(
                    github_repo=repo_name,
                    github_pr_number=pr_number,
                    plaky_task_id=task_id,
                    plaky_board_id=list_id,
                    origin="created",
                    cleanup_due_at=datetime.utcnow()
                    + timedelta(days=settings.pr_task_cleanup_ttl_days),
                )
            )
        # The PR named an issue that has no task yet: claim it for this card so the issue's own
        # events update this one instead of opening a second card for the same work.
        if orphan_issue_number:
            try:
                async with session.begin_nested():
                    session.add(
                        IssueTaskMap(
                            github_repo=repo_name,
                            github_issue_number=orphan_issue_number,
                            plaky_task_id=task_id,
                        )
                    )
                    await session.flush()
            except IntegrityError:
                pass  # the issue got its own task in the meantime; leave that one alone
        await c.add_comment(
            task_id,
            format_pr_notice_with_url(headline="**PR opened:**", pr_number=pr_number, pr_url=pr_url)
            + "\n\nBoardman created this task from the PR because no existing task matched.",
        )
        if amb.assign_qa:
            qa_res = await _assign_qa_for_pr(
                c,
                task_id=task_id,
                repo_full=full_name,
                pr_number=pr_number,
                pr_author_login=author_login,
                task_url=str(res.get("task_url") or ""),
                session=session,
            )
        else:
            qa_res = {"skipped": "ambiguous_pr.assign_qa is false"}
    session.add(
        SyncLog(
            action="pr_ambiguous_triage",
            github_repo=repo_name,
            github_ref=str(pr_number),
            plaky_task_id=task_id,
            detail=json.dumps(
                {
                    "pr_url": pr_url,
                    "full_name": full_name,
                    "task_type": task_type,
                    "assignee": author_id,
                    "qa": qa_res,
                },
                default=str,
            ),
        )
    )
    await session.commit()
    return {
        "ok": True,
        "ambiguous_triage": True,
        "created_from_pr": True,
        "plaky_task_id": task_id,
        "plaky_task_url": res.get("task_url"),
        "task_type": task_type,
        "assignee_clickup_id": author_id,
        "qa": qa_res,
    }


# -- metadata sync (edited) ----------------------------------------------------------------------


async def sync_pr_metadata(
    payload: PullRequestEventPayload,
    session: AsyncSession,
    task_ids: list[str],
    relink: Any,
    *,
    client: ClickUpClient | None = None,
) -> dict[str, Any]:
    """Re-sync type, priority, developer and (for drafts) status onto every task the PR is linked to."""
    from boardman.assignment.developer_eligibility import filter_developer
    from boardman.assignment.github_user_resolution import (
        github_actor_payload,
        resolve_github_user_to_user_id,
    )

    c = client or ClickUpClient()
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    state = resolve_pr_state(
        payload.pull_request, repo_full_name=payload.repository.full_name, repo_name=repo_name
    )
    engineer_id = ""
    if state.assignee_login:
        found = await resolve_github_user_to_user_id(
            github_actor_payload({"login": state.assignee_login})
        )
        engineer_id, _ = filter_developer(str(found or "").strip())

    # Cards this PR CREATED, and only those, follow the PR's own title and text; a card the
    # PR merely links to may hold something a person wrote.
    standalone_ids = {
        str(row.plaky_task_id)
        for row in (
            await session.execute(
                select(PullRequestTaskLink).where(
                    PullRequestTaskLink.github_repo == repo_name,
                    PullRequestTaskLink.github_pr_number == pr_number,
                    PullRequestTaskLink.github_issue_number == 0,
                    PullRequestTaskLink.link_source.in_(_PR_OWNED_LINK_SOURCES),
                )
            )
        ).scalars()
        if str(row.plaky_task_id).strip()
    }
    standalone_description = (
        f"Auto-created from GitHub PR: {state.url}\n\n"
        f"Repo: {state.repo_full_name}  Branch: {state.head_ref or '?'}  "
        f"Author: {state.author_login or 'unknown'}\n\n{state.body}"
    )

    results: list[dict[str, Any]] = []
    for task_id in task_ids:
        task = await read_task(c, task_id)
        kwargs: dict[str, Any] = {}
        if task_id in standalone_ids:
            if state.title and state.title != (task or {}).get("name"):
                kwargs["title"] = state.title
            if standalone_description != (task or {}).get("description"):
                kwargs["description"] = standalone_description
        if state.priority_explicit:
            kwargs["priority"] = state.priority
        # Fill-only: the assignee falls back to the PR author, so a manual reassignment must
        # survive every edit of the PR.
        if engineer_id and task is not None and not current_assignees(task):
            kwargs["add_assignee_ids"] = user_ids(engineer_id)
        # A draft means "assigned", never over work that has already moved on.
        if state.draft and task is not None:
            name = status_for_intent("workflow_assigned")
            if name and not status_intent_would_regress(intent_of(task), "workflow_assigned"):
                if name.casefold() != current_status(task).casefold():
                    kwargs["status"] = name
        mutation: dict[str, Any] = {"ok": True, "skipped": True}
        if kwargs:
            mutation = await c.update_task_fields(task_id, **kwargs)
            mutation.pop("task", None)
        type_ops: list[dict[str, Any]] = []
        if task is not None and state.task_type:
            type_ops = await sync_type_tag(c, task_id, task, state.task_type)
        results.append(
            {
                "task_id": task_id,
                "mutation": mutation,
                "type_ok": all(bool(o.get("ok")) for o in type_ops),
            }
        )
        stamp(
            session,
            "pr_metadata_synced",
            repo_name,
            pr_number,
            task_id,
            event=payload.action,
            task_type=state.task_type,
            priority=state.priority,
            clickup_ok=mutation.get("ok"),
        )
        # Backfill QA for a PR that was linked by some path other than a fresh `opened`.
        # `_assign_qa_for_pr` never overwrites an existing QA, so this is safe to repeat.
        if not state.draft:
            try:
                await _assign_qa_for_pr(
                    c,
                    task_id=task_id,
                    repo_full=state.repo_full_name,
                    pr_number=pr_number,
                    pr_author_login=state.author_login or "",
                    task_url=(task or {}).get("url") or "",
                    session=session,
                )
            except Exception as exc:  # noqa: BLE001 - a QA backfill miss must not break the sync
                _log.warning("QA backfill failed for PR #%s task %s: %s", pr_number, task_id, exc)
    await session.commit()
    failed = bool(results) and all(not r["mutation"].get("ok") for r in results)
    return {
        "ok": not failed,
        "skipped": failed,
        "event": "pr_metadata_synced",
        "updated": results,
        "relink": relink,
    }


# -- draft / ready / review request / push ---------------------------------------------------------


async def handle_pr_converted_to_draft(
    payload: PullRequestEventPayload,
    session: AsyncSession,
    *,
    client: ClickUpClient | None = None,
) -> dict[str, Any]:
    """Ready-for-review reversed: tasks sitting at "needs QA" go back to "in progress"."""
    c = client or ClickUpClient()
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    await ensure_links_live(payload, session)
    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no linked ClickUp tasks for this PR"}
    if not status_for_intent("workflow_needs_qa") or not status_for_intent("workflow_in_progress"):
        return {
            "ok": True,
            "skipped": True,
            "message": "needs-qa / in-progress statuses are not configured",
        }
    reverted: list[dict[str, Any]] = []
    for tid in task_ids:
        task = await read_task(c, tid)
        if intent_of(task) != "workflow_needs_qa":
            continue
        res = await set_intent_status(c, tid, "workflow_in_progress", task=task)
        stamp(session, "pr_converted_to_draft", repo_name, pr_number, tid, **{"from": "needs_qa"})
        reverted.append({"task_id": tid, "clickup": res})
    await session.commit()
    return {"ok": True, "updated": reverted, "event": "converted_to_draft"}


async def handle_pr_ready_for_review(
    payload: PullRequestEventPayload,
    session: AsyncSession,
    *,
    client: ClickUpClient | None = None,
) -> dict[str, Any]:
    """Draft to ready: move linked tasks to "needs QA" when that status is configured."""
    c = client or ClickUpClient()
    await ensure_links_live(payload, session)
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if not task_ids:
        linked = await get_linked_issue_numbers(
            payload.pull_request.body,
            repo_full_name=payload.repository.full_name,
            pr_title=payload.pull_request.title,
        )
        body_closes = await get_linked_issue_numbers(
            payload.pull_request.body, repo_full_name=payload.repository.full_name
        )
        for issue_num in linked:
            mapping = await find_plaky_task_by_issue(repo_name, issue_num, session)
            if not mapping:
                continue
            row = await upsert_pr_task_link(
                session,
                github_repo=repo_name,
                github_pr_number=pr_number,
                plaky_task_id=mapping.plaky_task_id,
                github_issue_number=int(issue_num),
                link_source=issue_link_source(int(issue_num), body_closes, linked),
            )
            if str(getattr(row, "link_source", "") or "") == _SUPERSEDED_LINK_SOURCE:
                continue
            task_ids.append(mapping.plaky_task_id)
        task_ids = list(dict.fromkeys(task_ids))
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no linked ClickUp tasks for this PR"}
    for tid in task_ids:
        await _maybe_set_needs_qa(c, tid, is_draft=False)
    await session.commit()
    return {"ok": True, "tasks": task_ids, "event": "ready_for_review"}


async def handle_pr_review_requested(
    payload: PullRequestEventPayload,
    session: AsyncSession,
    *,
    client: ClickUpClient | None = None,
) -> dict[str, Any]:
    """Asking for a review is not QA engaging, so only a withdrawn request moves the task.

    ``review_requested`` fires when Boardman itself asks the QA to review, so acting on it would
    move the task before anyone looked at it. ``review_request_removed`` puts the task back in the
    queue, but never over a verdict that is already in.
    """
    c = client or ClickUpClient()
    if payload.action != "review_request_removed":
        return {
            "ok": True,
            "skipped": True,
            "message": "asking for a review is not QA engaging; task stays at needs QA",
            "event": "review_requested",
        }
    await ensure_links_live(payload, session)
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no linked ClickUp tasks for this PR"}
    target = status_for_intent("workflow_needs_qa")
    if not target:
        return {"ok": True, "skipped": True, "message": "needs-QA status is not configured"}
    written: list[str] = []
    for tid in task_ids:
        res = await set_intent_status(
            c, tid, "workflow_needs_qa", protect=tuple(QA_VERDICT_INTENTS)
        )
        if res.get("ok") and not res.get("skipped"):
            written.append(tid)
    await session.commit()
    return {"ok": True, "tasks": written, "status": target, "event": "review_request_removed"}


async def handle_pr_synchronized(
    payload: PullRequestEventPayload,
    session: AsyncSession,
    *,
    client: ClickUpClient | None = None,
) -> dict[str, Any]:
    """New commits after a QA verdict: a few mean "revisions in progress", many mean "needs QA again"."""
    c = client or ClickUpClient()
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    await ensure_links_live(payload, session)
    try:
        from boardman.services.pr_review_nudges import parse_github_timestamp, record_activity

        pr_user = payload.pull_request.user or {}
        pusher = str(pr_user.get("login") or "") if isinstance(pr_user, dict) else ""
        raw = payload.pull_request.updated_at
        if pusher:
            await record_activity(
                session,
                github_repo=repo_name,
                github_pr_number=pr_number,
                actor_login=pusher,
                at=parse_github_timestamp(raw) if raw else datetime.utcnow(),
            )
    except Exception as exc:  # noqa: BLE001 - the nudge sweep is a bonus feature
        _log.warning(
            "pr_review_nudges.record_activity (push) failed for PR #%s: %s", pr_number, exc
        )

    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no linked ClickUp tasks for this PR"}

    from boardman.services.pr_task_registry import commits_at_last_review_for_pr

    baseline = await commits_at_last_review_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if baseline is None:
        return {"ok": True, "skipped": True, "message": "no QA verdict yet for this PR"}
    delta = payload.pull_request.commits - baseline
    if delta <= 0:
        return {"ok": True, "skipped": True, "message": "no new commits since last QA verdict"}

    escalate = delta > COMMITS_SINCE_REVIEW_ESCALATION_THRESHOLD
    if escalate:
        target_intent = (
            "workflow_needs_qa_again"
            if status_for_intent("workflow_needs_qa_again")
            else "workflow_needs_qa"
        )
        event, action = "resubmitted_needs_qa_again", "pr_resubmitted_needs_qa_again"
    else:
        target_intent = "workflow_in_progress"
        event, action = "revisions_in_progress", "pr_revisions_in_progress"
    target = status_for_intent(target_intent)
    if not target:
        return {
            "ok": True,
            "skipped": True,
            "message": f"{event}: target status is not configured",
        }
    # Only from a state that means "QA has weighed in" or "already mid-revision": a task parked
    # somewhere unrelated must not be dragged here by a new commit.
    from_intents = {
        i
        for i in (
            "github_pr_review_changes_requested",
            "github_pr_review_approved",
            "workflow_in_progress",
        )
        if status_for_intent(i)
    }
    if not from_intents:
        return {"ok": True, "skipped": True, "message": "no reviewed/in-progress status configured"}

    resumed: list[dict[str, Any]] = []
    for tid in task_ids:
        task = await read_task(c, tid)
        if intent_of(task) not in from_intents:
            continue
        res = await set_intent_status(c, tid, target_intent, task=task)
        stamp(
            session, action, repo_name, pr_number, tid, commits_since_review=delta, to_status=target
        )
        resumed.append({"task_id": tid, "clickup": res})
    await session.commit()
    return {"ok": True, "updated": resumed, "event": event}


# -- closed / merged / deployed ----------------------------------------------------------------------


async def handle_pr_closed_without_merge(
    payload: PullRequestEventPayload,
    session: AsyncSession,
    *,
    client: ClickUpClient | None = None,
) -> dict[str, Any]:
    """Withdraw the link; when that was the task's last open PR, review is over.

    A task parked in the review queue with nothing left to review is a lie on the board, so those
    (and only those) go back to "in progress". Verdicts and finished tasks are left alone.
    """
    c = client or ClickUpClient()
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    rows = await mark_pr_withdrawn(
        session,
        github_repo=repo_name,
        github_pr_number=pr_number,
        github_updated_at=str(getattr(payload.pull_request, "updated_at", "") or ""),
    )
    reverted: list[dict[str, Any]] = []
    review = {"workflow_needs_qa", "workflow_needs_qa_again", "workflow_in_qa"}
    if task_ids and status_for_intent("workflow_in_progress"):
        for tid in task_ids:
            if await has_any_open_pr_for_task(session, plaky_task_id=tid):
                continue
            task = await read_task(c, tid)
            if intent_of(task) not in review:
                continue
            reverted.append(
                {
                    "task_id": tid,
                    "clickup": await set_intent_status(c, tid, "workflow_in_progress", task=task),
                }
            )
    stamp(
        session,
        "pr_closed_without_merge",
        repo_name,
        pr_number,
        task_ids[0] if task_ids else "",
        withdrawn_links=len(rows),
        reverted=len(reverted),
    )
    await session.commit()
    return {"ok": True, "withdrawn_links": len(rows), "reverted": reverted}


async def handle_deployment_status(
    payload: DeploymentStatusEventPayload,
    session: AsyncSession,
    *,
    client: ClickUpClient | None = None,
) -> dict[str, Any]:
    """A successful deployment moves the merged task to "deployed", when that status is configured."""
    c = client or ClickUpClient()
    if (payload.deployment_status.state or "").strip().casefold() != "success":
        return {"ok": True, "skipped": True, "message": "deployment not successful (yet)"}
    if not status_for_intent("workflow_deployed"):
        return {"ok": True, "skipped": True, "message": "no deployed status configured"}
    repo_full = payload.repository.full_name
    repo_name = payload.repository.name
    sha = (payload.deployment.sha or "").strip()
    pr_numbers = await prs_for_commit_sha(repo_full, sha)
    if not pr_numbers:
        return {"ok": True, "skipped": True, "message": f"no PR found for commit {sha[:12]}"}
    updated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pr_number in pr_numbers:
        for tid in await distinct_task_ids_for_pr(
            session, github_repo=repo_name, github_pr_number=pr_number
        ):
            if tid in seen:
                continue
            seen.add(tid)
            res = await set_intent_status(c, tid, "workflow_deployed")
            stamp(
                session,
                "pr_deployed",
                repo_name,
                pr_number,
                tid,
                sha=sha,
                environment=payload.deployment.environment,
            )
            updated.append({"task_id": tid, "pr_number": pr_number, "clickup": res})
    await session.commit()
    return {"ok": True, "updated": updated, "event": "deployed"}


async def handle_pr_merged(
    payload: PullRequestEventPayload,
    session: AsyncSession,
    *,
    client: ClickUpClient | None = None,
) -> dict[str, Any]:
    """Merged: complete the tasks the PR's description says it closes, once, when no other PR is open."""
    c = client or ClickUpClient()
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    pr_url = payload.pull_request.html_url
    full_name = payload.repository.full_name

    linked = await get_linked_issue_numbers(
        payload.pull_request.body, repo_full_name=full_name, pr_title=payload.pull_request.title
    )
    # Only the description is a keyword GitHub acts on.
    body_closes = await get_linked_issue_numbers(
        payload.pull_request.body, repo_full_name=full_name
    )
    for issue_num in linked:
        mapping = await find_plaky_task_by_issue(repo_name, issue_num, session)
        if mapping:
            await upsert_pr_task_link(
                session,
                github_repo=repo_name,
                github_pr_number=pr_number,
                plaky_task_id=mapping.plaky_task_id,
                github_issue_number=int(issue_num),
                link_source=issue_link_source(int(issue_num), body_closes, linked),
            )
    merged_rows = await mark_pr_merged(session, github_repo=repo_name, github_pr_number=pr_number)

    affected: set[str] = {row.plaky_task_id for row in merged_rows}
    stated: set[str] = {
        str(row.plaky_task_id)
        for row in merged_rows
        if str(row.link_source or "") not in _WEAK_COMPLETION_LINK_SOURCES
    }
    # A card the PR itself opened is finished by the merge, unless it also became an issue's card:
    # then it answers to that issue, and only a keyword in the description counts.
    owned = {
        str(row.plaky_task_id)
        for row in merged_rows
        if str(row.link_source or "") in _PR_OWNED_LINK_SOURCES
    }
    if owned:
        claimed = await session.execute(
            select(IssueTaskMap).where(
                IssueTaskMap.github_repo == repo_name,
                IssueTaskMap.plaky_task_id.in_(sorted(owned)),
            )
        )
        for row in claimed.scalars():
            if int(row.github_issue_number) not in body_closes:
                stated.discard(str(row.plaky_task_id))
    for issue_num in linked:
        mapping = await find_plaky_task_by_issue(repo_name, issue_num, session)
        if mapping:
            affected.add(mapping.plaky_task_id)
            if int(issue_num) in body_closes:
                stated.add(str(mapping.plaky_task_id))

    if not affected:
        await remove_pr_row(payload.pull_request, payload.repository, session)
        await session.commit()
        return {"ok": True, "skipped": True, "message": "No linked ClickUp tasks for this PR"}

    merge_status = status_for_intent("workflow_completed")
    results: list[dict[str, Any]] = []
    for task_id in sorted(affected):
        if task_id not in stated:
            marker = f"pr-merged-not-completed:{repo_name}:{pr_number}:{task_id}"
            if not await comment_already_synced(session, "pr_merged_not_completed", marker):
                stamp(
                    session,
                    "pr_merged_not_completed",
                    repo_name,
                    pr_number,
                    task_id,
                    marker=marker,
                    pr_url=pr_url,
                    reason="not a closing keyword GitHub acts on",
                )
            results.append({"task_id": task_id, "completed": False, "reason": "weak_link"})
            continue
        if settings.complete_when_all_prs_merged and await has_any_open_pr_for_task(
            session, plaky_task_id=task_id
        ):
            results.append(
                {"task_id": task_id, "deferred": True, "reason": "other_prs_still_open_or_active"}
            )
            continue
        if not merge_status:
            results.append({"task_id": task_id, "skipped": "no completed status configured"})
            continue
        # Once per (PR, task): a merge is a one-time statement and the reconcile sweep replays every
        # merged PR it still sees, so re-writing "complete" would undo a person's later move.
        marker = f"pr-merged:{repo_name}:{pr_number}:{task_id}"
        if await comment_already_synced(session, "pr_merged", marker):
            results.append({"task_id": task_id, "status": merge_status, "already_applied": True})
            continue
        completion = await c.update_task_fields(task_id, status=merge_status)
        applied = bool(completion.get("ok"))
        # A refused write must not claim the identity that dedupes it, or every retry would skip.
        session.add(
            SyncLog(
                action="pr_merged" if applied else "pr_merged_failed",
                github_repo=repo_name,
                github_ref=str(pr_number),
                plaky_task_id=task_id,
                detail=json.dumps(
                    {
                        "marker": marker if applied else "",
                        "pr_url": pr_url,
                        "status": merge_status,
                        "clickup_ok": applied,
                        "all_prs_done": True,
                    }
                ),
            )
        )
        results.append({"task_id": task_id, "status": merge_status, "ok": applied})
    await remove_pr_row(payload.pull_request, payload.repository, session)
    await session.commit()
    return {"ok": True, "updated": results}


# -- review comments / labels -------------------------------------------------------------------------


async def handle_pr_review_comment(
    payload: PullRequestReviewCommentEventPayload,
    session: AsyncSession,
    *,
    client: ClickUpClient | None = None,
) -> dict[str, Any]:
    """Mirror an inline review comment to the linked tasks; the assigned QA's comment means "in QA"."""
    from boardman.assignment.github_user_resolution import (
        github_actor_payload,
        resolve_github_user_to_user_id,
    )
    from boardman.github.pr_actions import is_boardman_comment

    c = client or ClickUpClient()
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number if payload.pull_request else 0
    full_name = payload.repository.full_name if payload.repository else ""
    comment = payload.comment
    if not isinstance(comment, dict):
        return {"ok": True, "skipped": True, "message": "no comment payload"}
    commenter = comment.get("user")
    login = commenter.get("login") if isinstance(commenter, dict) else None
    if not login:
        return {"ok": False, "unretryable": True, "message": "No commenter login found"}

    task_ids: list[str] = []
    for issue_num in await get_linked_issue_numbers(
        payload.pull_request.body if payload.pull_request else None,
        repo_full_name=full_name,
        pr_title=payload.pull_request.title if payload.pull_request else None,
    ):
        mapping = await find_plaky_task_by_issue(repo_name, issue_num, session)
        if mapping:
            task_ids.append(str(mapping.plaky_task_id))
    if not task_ids:
        task_ids = list(
            await distinct_task_ids_for_pr(
                session, github_repo=repo_name, github_pr_number=pr_number
            )
        )
    task_ids = list(dict.fromkeys(task_ids))
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "No linked ClickUp tasks for this PR"}

    body = str(comment.get("body") or "").strip()
    if str(login).endswith("[bot]"):
        return {"ok": True, "skipped": True, "message": "bot review comment ignored"}
    if is_boardman_comment(body):
        return {"ok": True, "skipped": True, "message": "ignored Boardman's own comment"}
    marker = github_activity_marker(
        comment,
        kind="pr-review-comment",
        fallback=f"{repo_name}:{pr_number}:{login}:{body}",
    )
    is_revision = str(getattr(payload, "action", "") or "") == "edited"
    if is_revision and not edit_changed_the_text(payload):
        return {"ok": True, "skipped": True, "message": "edit did not change the comment text"}
    label = "GitHub inline review comment edited" if is_revision else "GitHub inline review comment"
    text = (
        f"**{label}** by `{login}` on PR #{pr_number}:\n\n"
        f"> {body[:1000].replace(chr(10), chr(10) + '> ')}"
    )
    url = str(comment.get("html_url") or "").strip()
    if url:
        text += f"\n\n{url}"
    mirrored = [
        await mirror_github_activity(
            session,
            c,
            task_id=tid,
            action="pr_review_comment_synced",
            marker=f"{marker}:{tid}",
            body=text,
            github_repo=repo_name,
            github_ref=str(pr_number),
            is_revision=is_revision,
            revision_body=body,
            edited_at=str(comment.get("updated_at") or "").strip(),
        )
        for tid in task_ids
    ]
    await session.commit()
    if is_revision:
        return {
            "ok": True,
            "event": "pr_review_comment_edit_mirrored",
            "mirrored": mirrored,
            "workflow_skipped": "an edited comment updates the record, not the state",
        }

    cfg = load_team_assignments()
    reviewer_id = next(
        (
            str(m.id)
            for m in cfg.members
            if (m.github_login or "").strip().casefold() == str(login).casefold()
        ),
        "",
    )
    if not reviewer_id:
        reviewer_id = str(
            await resolve_github_user_to_user_id(
                github_actor_payload(commenter if isinstance(commenter, dict) else {})
            )
            or ""
        )
    results: list[dict[str, Any]] = []
    for tid in task_ids:
        task = await read_task(c, tid)
        if task is None:
            continue
        assigned_qa = qa_from_field(task) or await stamped_qa(session, repo_name, pr_number, tid)
        if not (assigned_qa and reviewer_id and assigned_qa == reviewer_id):
            continue
        res = await set_intent_status(c, tid, "workflow_in_qa", task=task)
        if res.get("skipped") and "no ClickUp status" in str(res.get("skipped")):
            continue
        stamp(
            session,
            "in_qa_comment",
            repo_name,
            pr_number,
            tid,
            pr_url=payload.pull_request.html_url if payload.pull_request else "",
            commenter=login,
            status=res.get("status"),
        )
        results.append({"task_id": tid, "action": "in_qa_comment", "status": res.get("status")})
    await session.commit()
    return {"ok": True, "updated": results, "mirrored": mirrored}


async def handle_pr_labels_changed(
    payload: PullRequestEventPayload,
    session: AsyncSession,
    *,
    client: ClickUpClient | None = None,
) -> dict[str, Any]:
    """PR labeled/unlabeled: keep the linked tasks' type tag in step with the labels (labels only)."""
    from boardman.github.pr_signals import infer_task_type_from_pr, pr_label_names

    c = client or ClickUpClient()
    await ensure_links_live(payload, session)
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no ClickUp task linked for this PR"}
    labels = pr_label_names(getattr(payload.pull_request, "labels", None))
    label_type = infer_task_type_from_pr(None, labels)
    removed_label = getattr(payload, "label", None)
    removed_type = infer_task_type_from_pr(
        None, pr_label_names([removed_label]) if removed_label else []
    )
    if not label_type and not (payload.action == "unlabeled" and removed_type):
        return {"ok": True, "skipped": True, "message": "labels carry no type signal"}
    if not label_type:
        # A type label was removed and none remains. That is not a statement that the work is a
        # Feature, so leave the type as it is.
        return {
            "ok": True,
            "skipped": True,
            "message": "a type label was removed and none remains; leaving the type as it is",
        }
    pr_state = resolve_pr_state(
        payload.pull_request,
        repo_full_name=payload.repository.full_name,
        repo_name=repo_name,
    )
    updated: list[dict[str, Any]] = []
    for tid in task_ids:
        task = await read_task(c, tid)
        if task is None:
            updated.append({"task_id": tid, "ok": False, "message": "task unreadable"})
            continue
        ops = await sync_type_tag(c, tid, task, label_type)
        ok = all(bool(o.get("ok")) for o in ops)
        if pr_state.priority_explicit:
            ok = (
                bool((await c.update_task_fields(tid, priority=pr_state.priority)).get("ok")) and ok
            )
        updated.append({"task_id": tid, "ok": ok})
    stamp(
        session,
        "pr_labels_synced",
        repo_name,
        pr_number,
        task_ids[0],
        labels=labels,
        task_type=label_type,
        updated=updated,
    )
    await session.commit()
    return {"ok": True, "event": "pr_labels_synced", "task_type": label_type, "updated": updated}
