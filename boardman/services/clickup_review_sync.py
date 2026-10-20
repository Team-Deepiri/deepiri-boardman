"""GitHub PR reviews and PR comments to ClickUp. The ClickUp counterpart of ``pr_review_handler``;
that module dispatches here when ``TASK_PROVIDER=clickup``.

The rules it enforces, as on the Plaky path:

* An approval is a verdict on the code, not on the build: failing checks hold it back.
* "Request changes" counts only from the task's assigned QA; a comment from the assigned QA, or
  from a support-team member who is not the PR's author, means "in QA". Fail closed: when the PR
  author cannot be read, the roster alone no longer authorizes "in QA".
* A dev commenting after a QA verdict means "revisions in progress", never on a merged PR or a
  finished task. A dev pinging QA means "needs QA again". Anyone saying "pause" pauses the work.
* An edited comment updates the record, never the state. Bots and Boardman's own comments are
  ignored.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from boardman.assignment.config import load_team_assignments
from boardman.clickup.client import ClickUpClient
from boardman.clickup.statuses import status_for_intent
from boardman.github.pr_actions import is_boardman_comment
from boardman.github.support_qa import support_team_logins_casefold
from boardman.github.webhooks import IssueCommentEventPayload, PullRequestReviewEventPayload
from boardman.services.clickup_task_ops import (
    intent_of,
    qa_from_field,
    read_task,
    set_intent_status,
    stamp,
    stamped_qa,
)
from boardman.services.comment_dedupe import (
    edit_changed_the_text,
    github_activity_marker,
    mirror_github_activity,
)
from boardman.services.pr_review_common import (
    current_commit_count,
    failing_required_checks,
    pr_author_login,
    pr_is_merged,
    reviewer_id_from_roster,
)
from boardman.services.pr_task_registry import (
    distinct_task_ids_for_pr,
    stamp_commits_at_last_review,
)

_log = logging.getLogger(__name__)

_VERDICT_INTENTS = ("github_pr_review_changes_requested", "github_pr_review_approved")


async def _record_activity(
    session: AsyncSession, repo_name: str, pr_number: int, login: str, raw_time: str | None
) -> None:
    try:
        from boardman.services.pr_review_nudges import parse_github_timestamp, record_activity

        await record_activity(
            session,
            github_repo=repo_name,
            github_pr_number=pr_number,
            actor_login=login,
            at=parse_github_timestamp(raw_time) if raw_time else datetime.utcnow(),
        )
    except Exception as exc:  # noqa: BLE001 - the review-nudge sweep is a bonus feature
        _log.warning("pr_review_nudges.record_activity failed for PR #%s: %s", pr_number, exc)


async def _assigned_qa_id(
    session: AsyncSession, repo_name: str, pr_number: int, task_id: str, task: dict[str, Any] | None
) -> str:
    """The QA assigned to this task: the users field when configured, else the one on the link row."""
    return (qa_from_field(task) if task else "") or await stamped_qa(
        session, repo_name, pr_number, task_id
    )


async def _member_id(login: str, user: dict[str, Any]) -> str:
    """The ClickUp id for a GitHub user: the roster first, then the workspace lookup."""
    from boardman.assignment.github_user_resolution import (
        github_actor_payload,
        resolve_github_user_to_user_id,
    )

    rid = reviewer_id_from_roster(load_team_assignments(), login)
    if rid:
        return rid
    return str(await resolve_github_user_to_user_id(github_actor_payload(user)) or "")


def _roster_logins(cfg: Any) -> set[str]:
    return {
        (getattr(m, "github_login", "") or "").strip().casefold()
        for pool in (cfg.members, getattr(cfg, "fallback_members", []) or [])
        for m in pool
        if (getattr(m, "github_login", "") or "").strip()
    }


async def _apply_to_tasks(
    session: AsyncSession,
    c: ClickUpClient,
    task_ids: list[str],
    intent: str,
    *,
    repo_name: str,
    pr_number: int,
    action: str,
    **detail: Any,
) -> list[dict[str, Any]]:
    """Write the status for ``intent`` on every task, logging each one."""
    updated: list[dict[str, Any]] = []
    for tid in task_ids:
        res = await set_intent_status(c, tid, intent)
        stamp(
            session,
            action,
            repo_name,
            pr_number,
            tid,
            status=res.get("status"),
            clickup_ok=res.get("ok"),
            **detail,
        )
        updated.append({"task_id": tid, "clickup": res})
    return updated


# -- reviews ---------------------------------------------------------------------------------------


async def _handle_review_dismissed(
    payload: PullRequestReviewEventPayload, session: AsyncSession, c: ClickUpClient
) -> dict[str, Any]:
    """A dismissed approval must not leave the task looking approved: back to "in QA"."""
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no ClickUp task linked for this PR"}
    if not status_for_intent("github_pr_review_approved") or not status_for_intent(
        "workflow_in_qa"
    ):
        return {
            "ok": True,
            "skipped": True,
            "message": "approved / in-qa statuses are not configured",
        }
    reverted: list[dict[str, Any]] = []
    for tid in task_ids:
        task = await read_task(c, tid)
        if intent_of(task) != "github_pr_review_approved":
            continue
        res = await set_intent_status(c, tid, "workflow_in_qa", task=task)
        stamp(session, "pr_review_dismissed", repo_name, pr_number, tid, **{"from": "qa_approved"})
        reverted.append({"task_id": tid, "clickup": res})
    await session.commit()
    return {"ok": True, "updated": reverted, "event": "review_dismissed"}


async def handle_pull_request_review(
    payload: PullRequestReviewEventPayload,
    session: AsyncSession,
    *,
    client: ClickUpClient | None = None,
) -> dict[str, Any]:
    c = client or ClickUpClient()
    action = (payload.action or "").strip().casefold()
    if action == "dismissed":
        return await _handle_review_dismissed(payload, session, c)
    if action != "submitted":
        return {"ok": True, "message": "ignored non-submitted review"}

    repo_name = payload.repository.name
    full_name = payload.repository.full_name
    pr_number = payload.pull_request.number
    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no ClickUp task linked for this PR"}

    review_user: dict[str, Any] = (
        payload.review.user if isinstance(payload.review.user, dict) else {}
    )
    reviewer = str(review_user.get("login") or "").strip()
    state = (payload.review.state or "").strip().casefold()
    on_support_roster = bool(reviewer) and reviewer.casefold() in support_team_logins_casefold()
    if reviewer and not reviewer.endswith("[bot]"):
        await _record_activity(session, repo_name, pr_number, reviewer, payload.review.submitted_at)

    review_data = payload.review.model_dump()
    body = str(review_data.get("body") or "").strip()
    marker = github_activity_marker(
        review_data,
        kind="pr-review",
        fallback=f"{repo_name}:{pr_number}:{reviewer}:{state}:{body}",
    )
    # A review bot leaves a summary on every push; mirroring those buries the human review.
    if body and not reviewer.endswith("[bot]"):
        url = str(review_data.get("html_url") or "").strip()
        text = (
            f"**GitHub PR review** by `{reviewer or 'unknown'}` on PR #{pr_number}:\n\n"
            f"> {body[:1000].replace(chr(10), chr(10) + '> ')}"
        )
        if url:
            text += f"\n\n{url}"
        for tid in task_ids:
            await mirror_github_activity(
                session,
                c,
                task_id=tid,
                action="pr_review_synced",
                marker=f"{marker}:{tid}",
                body=text,
                github_repo=repo_name,
                github_ref=str(pr_number),
            )
        await session.commit()

    # approved: any reviewer's approval. changes_requested: only the assigned QA's.
    # commented: "in QA" only for support-team members or the assigned QA, to avoid drive-by noise.
    intent = ""
    only_assigned_qa = False
    reviewer_id = ""
    if state == "approved":
        failing = await failing_required_checks(full_name, pr_number)
        if failing:
            stamp(
                session,
                "approval_held_ci_failing",
                repo_name,
                pr_number,
                task_ids[0],
                reviewer=reviewer,
                failing=failing[:8],
            )
            await session.commit()
            return {
                "ok": True,
                "skipped": True,
                "message": (
                    "approval received but NOT applied to the board: failing checks "
                    f"({', '.join(failing[:5])}). Re-approve or re-run checks once green."
                ),
                "reviewer": reviewer,
                "state": state,
            }
        intent = "github_pr_review_approved"
    elif state == "changes_requested":
        only_assigned_qa = True
        intent = "github_pr_review_changes_requested"
        if status_for_intent(intent):
            reviewer_id = await _member_id(reviewer, review_user)
    elif state == "commented":
        authorized = on_support_roster
        if not authorized and reviewer:
            rid = await _member_id(reviewer, review_user)
            task = await read_task(c, task_ids[0])
            assigned = await _assigned_qa_id(session, repo_name, pr_number, task_ids[0], task)
            authorized = bool(rid) and bool(assigned) and assigned == rid
        if authorized:
            intent = "workflow_in_qa"

    if not intent or not status_for_intent(intent):
        return {
            "ok": True,
            "skipped": True,
            "message": "no matching QA status for this review (set the CLICKUP_STATUS_* settings)",
            "reviewer": reviewer,
            "state": state,
        }
    if only_assigned_qa and not reviewer_id:
        return {
            "ok": True,
            "skipped": True,
            "message": "changes_requested ignored: could not map reviewer to a ClickUp user id",
            "reviewer": reviewer,
            "state": state,
        }

    updated: list[dict[str, Any]] = []
    for tid in task_ids:
        if only_assigned_qa:
            task = await read_task(c, tid)
            assigned = await _assigned_qa_id(session, repo_name, pr_number, tid, task)
            if not assigned or assigned != reviewer_id:
                continue
        res = await set_intent_status(c, tid, intent)
        stamp(
            session,
            "pr_review_clickup_status",
            repo_name,
            pr_number,
            tid,
            review_state=state,
            reviewer=reviewer,
            clickup_status=res.get("status"),
            clickup_ok=res.get("ok"),
            assigned_qa_only=only_assigned_qa,
        )
        updated.append({"task_id": tid, "clickup": res})

    if only_assigned_qa and not updated:
        await session.commit()
        return {
            "ok": True,
            "skipped": True,
            "message": "changes_requested ignored: reviewer is not the assigned QA on linked task(s)",
            "reviewer": reviewer,
            "state": state,
            "updated": [],
        }
    if updated and state in ("approved", "changes_requested"):
        # Baseline for the push handler: commits after this verdict tell "revisions in progress"
        # from "needs QA again". Fetch it fresh; the review payload's PR object may not carry it.
        commits = await current_commit_count(full_name, pr_number)
        if commits is not None:
            await stamp_commits_at_last_review(
                session, github_repo=repo_name, github_pr_number=pr_number, commits=commits
            )
    await session.commit()
    return {"ok": True, "updated": updated, "status": status_for_intent(intent)}


# -- comments ----------------------------------------------------------------------------------------


def _comment_fields(payload: IssueCommentEventPayload) -> dict[str, Any]:
    comment = payload.comment if isinstance(payload.comment, dict) else {}
    user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
    return {
        "user": user,
        "login": str(user.get("login") or "").strip(),
        "body": str(comment.get("body") or ""),
        "url": str(comment.get("html_url") or "").strip(),
        "edited_at": str(comment.get("updated_at") or "").strip(),
        "created_at": str(comment.get("created_at") or "").strip(),
    }


async def sync_plain_issue_comment(
    payload: IssueCommentEventPayload,
    session: AsyncSession,
    *,
    is_revision: bool = False,
    client: ClickUpClient | None = None,
) -> dict[str, Any]:
    """A comment on a plain GitHub issue lands on the issue's ClickUp task."""
    from boardman.services.issue_handler import find_plaky_task_by_issue

    c = client or ClickUpClient()
    repo_name = payload.repository.name
    number = payload.issue.number
    f = _comment_fields(payload)
    body = f["body"].strip()
    if f["login"].endswith("[bot]"):
        return {"ok": True, "skipped": True, "message": "bot comment ignored"}
    if not body:
        return {"ok": True, "skipped": True, "message": "empty comment body"}
    mapping = await find_plaky_task_by_issue(repo_name, number, session)
    if not mapping or not mapping.plaky_task_id:
        return {"ok": True, "skipped": True, "message": "no ClickUp task mapped for this issue"}

    excerpt = body[:700] + ("…" if len(body) > 700 else "")
    label = "GitHub comment edited" if is_revision else "GitHub comment"
    text = (
        f"**{label}** by `{f['login'] or 'unknown'}` on issue #{number}:\n\n"
        f"> {excerpt.replace(chr(10), chr(10) + '> ')}"
    )
    if f["url"]:
        text += f"\n\n{f['url']}"
    res = await mirror_github_activity(
        session,
        c,
        task_id=str(mapping.plaky_task_id),
        action="issue_comment_synced",
        marker=github_activity_marker(
            payload.comment,
            kind="issue-comment",
            fallback=f"{repo_name}:{number}:{f['login']}:{body}",
        ),
        body=text,
        github_repo=repo_name,
        github_ref=str(number),
        is_revision=is_revision,
        revision_body=body,
        edited_at=f["edited_at"],
    )
    await session.commit()
    return {
        "ok": res.get("ok", True),
        "skipped": bool(res.get("skipped")),
        "plaky_task_id": mapping.plaky_task_id,
        "event": "issue_comment_synced",
        "mirrored": res.get("mirrored", False),
    }


async def handle_issue_comment_on_pr(
    payload: IssueCommentEventPayload,
    session: AsyncSession,
    *,
    client: ClickUpClient | None = None,
) -> dict[str, Any]:
    from boardman.github.pr_signals import comment_mentions_qa_or_support, comment_requests_pause

    c = client or ClickUpClient()
    if payload.action not in ("created", "edited"):
        return {"ok": True, "message": f"ignored {payload.action} comment"}
    is_revision = payload.action == "edited"
    if is_revision and not edit_changed_the_text(payload):
        return {"ok": True, "skipped": True, "message": "edit did not change the comment text"}
    f = _comment_fields(payload)
    # Boardman posts as the PAT owner, usually a support-team member, so its own QA-assignment
    # comment must never drive the state machine.
    if is_boardman_comment(f["body"]):
        return {"ok": True, "skipped": True, "message": "ignored Boardman's own comment"}
    if not payload.issue.pull_request:
        return await sync_plain_issue_comment(payload, session, is_revision=is_revision, client=c)

    repo_name = payload.repository.name
    full_name = payload.repository.full_name
    pr_number = payload.issue.number
    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no ClickUp task linked for this PR"}
    login, body = f["login"], f["body"]
    if login.endswith("[bot]"):
        return {"ok": True, "skipped": True, "message": "bot comment ignored"}
    if login:
        await _record_activity(session, repo_name, pr_number, login, f["created_at"])

    excerpt = body[:700] + ("…" if len(body) > 700 else "")
    label = "GitHub PR comment edited" if is_revision else "GitHub PR comment"
    text = (
        f"**{label}** by `{login or 'unknown'}` on PR #{pr_number}:\n\n"
        f"> {excerpt.replace(chr(10), chr(10) + '> ')}"
    )
    if f["url"]:
        text += f"\n\n{f['url']}"
    marker = github_activity_marker(
        payload.comment,
        kind="pr-conversation-comment",
        fallback=f"{repo_name}:{pr_number}:{login}:{body}",
    )
    mirrored = [
        await mirror_github_activity(
            session,
            c,
            task_id=tid,
            action="pr_comment_synced",
            marker=f"{marker}:{tid}",
            body=text,
            github_repo=repo_name,
            github_ref=str(pr_number),
            is_revision=is_revision,
            revision_body=body,
            edited_at=f["edited_at"],
        )
        for tid in task_ids
    ]
    await session.commit()
    if is_revision:
        return {
            "ok": True,
            "event": "pr_comment_edit_mirrored",
            "mirrored": mirrored,
            "workflow_skipped": "an edited comment updates the record, not the state",
        }

    # Anyone saying "pause" pauses the work.
    if comment_requests_pause(body):
        if not status_for_intent("workflow_paused"):
            return {
                "ok": True,
                "skipped": True,
                "message": "pause requested but no paused status configured",
            }
        updated = await _apply_to_tasks(
            session,
            c,
            task_ids,
            "workflow_paused",
            repo_name=repo_name,
            pr_number=pr_number,
            action="pr_comment_paused",
            commenter=login,
        )
        await session.commit()
        return {
            "ok": True,
            "updated": updated,
            "status": status_for_intent("workflow_paused"),
            "event": "paused",
        }

    cfg = load_team_assignments()
    support = support_team_logins_casefold()
    roster = _roster_logins(cfg)
    qa_side = bool(login) and login.casefold() in (support | roster)

    # A dev pinging QA or the support team means "needs QA again".
    if (
        "@" in body
        and not qa_side
        and comment_mentions_qa_or_support(body, support, qa_logins=roster)
    ):
        intent = (
            "workflow_needs_qa_again"
            if status_for_intent("workflow_needs_qa_again")
            else "workflow_needs_qa"
        )
        if status_for_intent(intent):
            updated = await _apply_to_tasks(
                session,
                c,
                task_ids,
                intent,
                repo_name=repo_name,
                pr_number=pr_number,
                action="pr_comment_needs_qa_again",
                commenter=login,
            )
            await session.commit()
            return {
                "ok": True,
                "updated": updated,
                "status": status_for_intent(intent),
                "event": "needs_qa_again",
            }

    if not status_for_intent("workflow_in_qa"):
        return {"ok": True, "skipped": True, "message": "in_qa status is not configured"}

    # "In QA" is only justified when the commenter IS QA: the assigned QA, or a support or roster
    # member covering for them. The PR's own author never counts, even when on the roster.
    pr_author = ""
    is_pr_author = False
    authorizes_from_side = False
    if qa_side:
        pr_author = (await pr_author_login(full_name, pr_number)).casefold()
        is_pr_author = bool(login) and login.casefold() == pr_author
        authorizes_from_side = not is_pr_author and bool(pr_author)
    member_id = await _member_id(login, f["user"]) if login else ""
    is_assigned_qa = False
    tasks_by_id: dict[str, dict[str, Any] | None] = {}
    for tid in task_ids:
        tasks_by_id[tid] = await read_task(c, tid)
        if member_id and member_id == await _assigned_qa_id(
            session, repo_name, pr_number, tid, tasks_by_id[tid]
        ):
            is_assigned_qa = True
            break

    # A non-QA comment on a task already at a QA verdict means the dev is addressing feedback:
    # "revisions in progress" instead of "in QA". Never on a merged PR or a finished task.
    if not is_assigned_qa and status_for_intent("workflow_in_progress"):
        verdicts = {i for i in _VERDICT_INTENTS if status_for_intent(i)}
        if verdicts and not await pr_is_merged(full_name, pr_number):
            resumed: list[dict[str, Any]] = []
            for tid in task_ids:
                task = tasks_by_id.get(tid) or await read_task(c, tid)
                # Only a task sitting at a QA verdict resumes. A finished task is not one, so a
                # late comment can never pull it back.
                if intent_of(task) not in verdicts:
                    continue
                res = await set_intent_status(c, tid, "workflow_in_progress", task=task)
                stamp(
                    session,
                    "pr_comment_resumed_in_progress",
                    repo_name,
                    pr_number,
                    tid,
                    commenter=login,
                    to_status=res.get("status"),
                )
                resumed.append({"task_id": tid, "clickup": res})
            if resumed:
                await session.commit()
                return {
                    "ok": True,
                    "updated": resumed,
                    "status": status_for_intent("workflow_in_progress"),
                    "event": "revisions_in_progress",
                }

    if not is_assigned_qa and not authorizes_from_side:
        return {
            "ok": True,
            "skipped": True,
            "message": "commenter is not the assigned QA or (support member who is not the PR author)",
            "commenter": login,
            "pr_author_matches_commenter": is_pr_author,
        }

    updated = await _apply_to_tasks(
        session,
        c,
        task_ids,
        "workflow_in_qa",
        repo_name=repo_name,
        pr_number=pr_number,
        action="pr_comment_in_qa",
        commenter=login,
    )
    await session.commit()
    return {
        "ok": True,
        "updated": updated,
        "status": status_for_intent("workflow_in_qa"),
        "mirrored": mirrored,
    }
