"""GitHub PR reviews + PR comments → Plaky QA statuses."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from boardman.assignment.config import TeamAssignmentsConfig, load_team_assignments
from boardman.database.models import SyncLog
from boardman.github.pr_actions import is_boardman_comment
from boardman.github.repo_fetch import fetch_pr_assignees_and_reviewers_logins
from boardman.github.support_qa import support_team_logins_casefold
from boardman.github.webhooks import IssueCommentEventPayload, PullRequestReviewEventPayload
from boardman.observability.degradation import log_degraded
from boardman.plaky.board_schema import plaky_item_person_ids, plaky_item_status_id
from boardman.plaky.client import PlakyClient
from boardman.plaky.dynamic_qa_status import (
    github_actor_payload,
    resolve_github_user_to_plaky_user_id,
    resolve_plaky_status_patch,
    resolve_qa_assignee_field_key,
)
from boardman.repos_config import get_routing_async
from boardman.services.comment_dedupe import (
    edit_changed_the_text,
    github_activity_marker,
    mirror_github_activity,
)
from boardman.services.pr_handler import _update_plaky_task_status
from boardman.services.pr_task_registry import (
    distinct_task_ids_for_pr,
    stamp_commits_at_last_review,
)
from boardman.services.webhook_side_effects import maybe_enqueue_plaky_reorder_after_task
from boardman.settings import settings

_log = logging.getLogger(__name__)


def _qa_approved_status() -> str:
    return (settings.plaky_pr_qa_approved_status or settings.plaky_status_qa_approved or "").strip()


def _qa_rejected_status() -> str:
    return (settings.plaky_pr_qa_rejected_status or settings.plaky_status_qa_rejected or "").strip()


def _in_qa_status() -> str:
    return (settings.plaky_pr_in_qa_status or settings.plaky_status_in_qa or "").strip()


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


async def _resolve_status(board_id: str, env_value: str, *intents: str) -> tuple[str | None, str]:
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
    return await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )


def _reviewer_plaky_id_from_roster(cfg: TeamAssignmentsConfig, reviewer_login: str) -> str | None:
    if not reviewer_login:
        return None
    # fallback_members too: the bug specialist lives only in the yaml fallback, and her
    # reviews/comments must carry the same authority as any rostered QA's.
    for pool in (cfg.members, getattr(cfg, "fallback_members", []) or []):
        for m in pool:
            gl = (getattr(m, "github_login", None) or "").strip()
            if gl and gl.casefold() == reviewer_login.casefold():
                mid = (getattr(m, "id", None) or "").strip()
                if mid:
                    return mid
    return None


async def _failing_required_checks(full_name: str, pr_number: int) -> list[str]:
    """Names of failing check runs on the PR head commit, [] when green or unknowable.

    An approval is a verdict on the code, not on the build. If required checks are red,
    marking the task QA Verified would present broken work as done. API trouble returns
    [] on purpose: absence of the signal is not evidence of failure.
    """
    if not (settings.github_pat or "").strip():
        return []
    try:
        from boardman.github.http import github_http_client

        client = github_http_client()
        hdr = {
            "Authorization": f"Bearer {settings.github_pat}",
            "Accept": "application/vnd.github+json",
        }
        r = await client.get(
            f"https://api.github.com/repos/{full_name}/pulls/{int(pr_number)}", headers=hdr
        )
        if r.status_code != 200:
            return []
        sha = str(((r.json().get("head") or {}) or {}).get("sha") or "")
        if not sha:
            return []
        r2 = await client.get(
            f"https://api.github.com/repos/{full_name}/commits/{sha}/check-runs?per_page=100",
            headers=hdr,
        )
        if r2.status_code != 200:
            return []
        bad: list[str] = []
        for run in r2.json().get("check_runs") or []:
            if not isinstance(run, dict):
                continue
            if str(run.get("status") or "") == "completed" and str(
                run.get("conclusion") or ""
            ).lower() in ("failure", "timed_out", "action_required"):
                bad.append(str(run.get("name") or "check"))
        return bad
    except Exception:  # noqa: BLE001 - graceful degradation
        log_degraded(_log, "_failing_required_checks: GET /commits/{ref}/check-runs")
        return []


async def _assigned_qa_plaky_id(
    plaky: PlakyClient,
    board_id: str,
    task_id: str,
    qa_field: str,
) -> str:
    task_info = await plaky.get_board_item_public((board_id or "").strip(), task_id)
    if not task_info.get("ok") or not task_info.get("item"):
        return ""
    ids = plaky_item_person_ids(task_info["item"], qa_field)
    return ids[0] if ids else ""


async def _handle_review_dismissed(
    payload: PullRequestReviewEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    """A dismissed approval must not leave the task looking QA-approved → back to In QA."""
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    task_ids = await _task_ids_for_pr(session, repo_name, pr_number)
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no Plaky task linked for this PR"}

    routing = await get_routing_async(payload.repository.full_name, repo_name, settings.github_org)
    bid = ((routing.plaky_board_id if routing and routing.plaky_board_id else "") or "").strip()
    if not bid:
        return {
            "ok": True,
            "skipped": True,
            "message": "no board id for repo; cannot verify status",
        }

    approved = await resolve_plaky_status_patch(bid, intent="github_pr_review_approved")
    in_qa = await resolve_plaky_status_patch(bid, intent="workflow_in_qa")
    if not approved or not in_qa:
        return {
            "ok": True,
            "skipped": True,
            "message": "qa-approved / in-qa status not resolvable from board",
        }
    ap_key, ap_id = approved
    iq_key, iq_id = in_qa

    from boardman.services.pr_handler import _current_status_value

    plaky = PlakyClient()
    reverted: list[dict[str, Any]] = []
    for tid in task_ids:
        current = await _current_status_value(plaky, bid, tid, ap_key)
        if not current or current != str(ap_id):
            continue
        res = await _update_plaky_task_status(tid, iq_id, bid, status_field_key=iq_key)
        session.add(
            SyncLog(
                action="pr_review_dismissed",
                github_repo=repo_name,
                github_ref=str(pr_number),
                plaky_task_id=tid,
                detail=json.dumps({"from": "qa_approved", "to_status": iq_id}, default=str),
            )
        )
        reverted.append({"task_id": tid, "plaky": res})

    await session.commit()
    if reverted:
        await maybe_enqueue_plaky_reorder_after_task(plaky, reverted[0]["task_id"])
    return {"ok": True, "updated": reverted, "event": "review_dismissed"}


async def handle_pull_request_review(
    payload: PullRequestReviewEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    action = (payload.action or "").strip().casefold()
    if action == "dismissed":
        return await _handle_review_dismissed(payload, session)
    if action != "submitted":
        return {"ok": True, "message": "ignored non-submitted review"}

    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    routing = await get_routing_async(payload.repository.full_name, repo_name, settings.github_org)
    board_id = (routing.plaky_board_id if routing and routing.plaky_board_id else "") or ""

    task_ids = await _task_ids_for_pr(session, repo_name, pr_number)
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no Plaky task linked for this PR"}

    review_user: dict[str, Any] = (
        payload.review.user if isinstance(payload.review.user, dict) else {}
    )
    reviewer_login = str(review_user.get("login") or "").strip()

    state = (payload.review.state or "").strip().casefold()
    support = support_team_logins_casefold()
    on_support_roster = bool(reviewer_login) and reviewer_login.casefold() in support

    bid = (board_id or "").strip()
    plaky = PlakyClient()
    updated: list[dict[str, Any]] = []
    review_data = payload.review.model_dump()
    review_body = str(review_data.get("body") or "").strip()
    review_marker = github_activity_marker(
        review_data,
        kind="pr-review",
        fallback=f"{repo_name}:{pr_number}:{reviewer_login}:{state}:{review_body}",
    )
    review_mirrors: list[dict[str, Any]] = []
    # A review bot leaves a summary on every push. Mirroring those buries the human
    # review the QA reviewer is looking for; the bot's verdict still drives nothing here,
    # since authorization is checked separately below.
    if review_body and not reviewer_login.endswith("[bot]"):
        review_url = str(review_data.get("html_url") or "").strip()
        review_text = (
            f"📝 **GitHub PR review** by `{reviewer_login or 'unknown'}` on PR #{pr_number}:\n\n"
            f"> {review_body[:1000].replace(chr(10), chr(10) + '> ')}"
        )
        if review_url:
            review_text += f"\n\n{review_url}"
        for task_id in task_ids:
            review_mirrors.append(
                await mirror_github_activity(
                    session,
                    plaky,
                    task_id=task_id,
                    action="pr_review_synced",
                    marker=f"{review_marker}:{task_id}",
                    body=review_text,
                    board_id=bid,
                    github_repo=repo_name,
                    github_ref=str(pr_number),
                )
            )
        await session.commit()

    # Approved: any reviewer's Approve → QA verified / approved Plaky status.
    # changes_requested: only the Plaky-assigned QA's "Request changes" → QA rejected (not other reviewers).
    # "commented" → In QA only for support-team logins to avoid noise from drive-by comments.
    target_status = ""
    status_field_key: str | None = None
    changes_requested_only_assigned_qa = False
    reviewer_plaky_id: str | None = None
    qa_field_for_changes: str = ""

    if state == "approved":
        failing = await _failing_required_checks(payload.repository.full_name, pr_number)
        if failing:
            session.add(
                SyncLog(
                    action="approval_held_ci_failing",
                    github_repo=repo_name,
                    github_ref=str(pr_number),
                    plaky_task_id=task_ids[0],
                    detail=json.dumps({"reviewer": reviewer_login, "failing": failing[:8]}),
                )
            )
            await session.commit()
            return {
                "ok": True,
                "skipped": True,
                "message": (
                    "approval received but NOT applied to the board: failing checks "
                    f"({', '.join(failing[:5])}). Re-approve or re-run checks once green."
                ),
                "reviewer": reviewer_login,
                "state": state,
            }
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
    elif state == "commented":
        authorized = on_support_roster
        if not authorized and reviewer_login and bid and task_ids:
            # The task's ASSIGNED QA commenting means QA is actively working it — In QA —
            # even when they are not on the GitHub support team (the bug specialist).
            cfg_c = load_team_assignments()
            rid = _reviewer_plaky_id_from_roster(cfg_c, reviewer_login)
            if not rid:
                rid = await resolve_github_user_to_plaky_user_id(github_actor_payload(review_user))
            if rid:
                qa_field_c = await resolve_qa_assignee_field_key(bid, cfg_c.plaky_field_qa)
                if qa_field_c:
                    rid_assigned = await _assigned_qa_plaky_id(plaky, bid, task_ids[0], qa_field_c)
                    authorized = bool(rid_assigned) and rid_assigned == str(rid)
        if authorized:
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

    if updated and state in ("approved", "changes_requested"):
        # Baseline for handle_pr_synchronized: commits pushed AFTER this verdict are
        # what distinguish Revisions In Progress from Needs QA Again. The review
        # payload embeds a slim `pull_request` (see GitHubPullRequest docstring) that
        # may not carry a live commit count, so fetch it fresh rather than trust it.
        commits_now = await _current_commit_count(payload.repository.full_name, pr_number)
        if commits_now is not None:
            await stamp_commits_at_last_review(
                session,
                github_repo=repo_name,
                github_pr_number=pr_number,
                commits=commits_now,
            )

    await session.commit()
    if updated and task_ids:
        await maybe_enqueue_plaky_reorder_after_task(plaky, task_ids[0])
    return {"ok": True, "updated": updated, "status": target_status}


async def _current_commit_count(full_name: str, pr_number: int) -> int | None:
    """Live commit count on the PR right now — None when unknowable (no PAT, API
    trouble), so callers skip stamping rather than recording a wrong baseline."""
    if not (settings.github_pat or "").strip():
        return None
    try:
        from boardman.github.http import github_http_client

        client = github_http_client()
        hdr = {
            "Authorization": f"Bearer {settings.github_pat}",
            "Accept": "application/vnd.github+json",
        }
        r = await client.get(
            f"https://api.github.com/repos/{full_name}/pulls/{int(pr_number)}", headers=hdr
        )
        if r.status_code != 200:
            return None
        commits = r.json().get("commits")
        return int(commits) if isinstance(commits, int) else None
    except Exception:  # noqa: BLE001 - a missing baseline degrades to "can't escalate yet"
        return None


async def _sync_plain_issue_comment(
    payload: IssueCommentEventPayload,
    session: AsyncSession,
    *,
    is_revision: bool = False,
) -> dict[str, Any]:
    """Comments on a plain GitHub issue land on the linked Plaky task (QA discussion in one place)."""
    from boardman.services.issue_handler import find_plaky_task_by_issue

    repo_name = payload.repository.name
    issue_number = payload.issue.number

    commenter = ""
    comment_body = ""
    comment_url = ""
    comment_edited_at = ""
    if isinstance(payload.comment, dict):
        u = payload.comment.get("user")
        if isinstance(u, dict):
            commenter = str(u.get("login") or "").strip()
        comment_body = str(payload.comment.get("body") or "").strip()
        comment_url = str(payload.comment.get("html_url") or "").strip()
        comment_edited_at = str(payload.comment.get("updated_at") or "").strip()
    if commenter.endswith("[bot]"):
        return {"ok": True, "skipped": True, "message": "bot comment ignored"}
    if not comment_body:
        return {"ok": True, "skipped": True, "message": "empty comment body"}

    mapping = await find_plaky_task_by_issue(repo_name, issue_number, session)
    if not mapping or not mapping.plaky_task_id:
        return {"ok": True, "skipped": True, "message": "no Plaky task mapped for this issue"}

    routing = await get_routing_async(payload.repository.full_name, repo_name, settings.github_org)
    bid = ((routing.plaky_board_id if routing and routing.plaky_board_id else "") or "").strip()

    excerpt = comment_body[:700] + ("…" if len(comment_body) > 700 else "")
    quoted = "> " + excerpt.replace("\n", "\n> ")
    label = "GitHub comment edited" if is_revision else "GitHub comment"
    text = f"💬 **{label}** by `{commenter or 'unknown'}` on issue #{issue_number}:\n\n{quoted}"
    if comment_url:
        text += f"\n\n{comment_url}"

    plaky = PlakyClient()
    marker = github_activity_marker(
        payload.comment,
        kind="issue-comment",
        fallback=f"{repo_name}:{issue_number}:{commenter}:{comment_body}",
    )
    res = await mirror_github_activity(
        session,
        plaky,
        task_id=mapping.plaky_task_id,
        action="issue_comment_synced",
        marker=marker,
        body=text,
        board_id=bid,
        github_repo=repo_name,
        github_ref=str(issue_number),
        is_revision=is_revision,
        revision_body=comment_body,
        edited_at=comment_edited_at,
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
) -> dict[str, Any]:
    # `edited` is handled too: a comment corrected on GitHub used to be dropped here, so
    # the board kept showing the wrong text with no sign anything had changed. The mirror
    # is keyed on (comment, wording), so an edit lands once and redeliveries land never.
    if payload.action not in ("created", "edited"):
        return {"ok": True, "message": f"ignored {payload.action} comment"}
    is_revision = payload.action == "edited"
    if is_revision and not edit_changed_the_text(payload):
        return {"ok": True, "skipped": True, "message": "edit did not change the comment text"}

    # Boardman's own comments must never drive the state machine. It posts as the PAT
    # owner — usually a support-team member — so without this its QA-assignment comment
    # comes back as "support team commented on the PR" and moves the task to In QA
    # before the assigned QA has opened it.
    if isinstance(payload.comment, dict) and is_boardman_comment(
        str(payload.comment.get("body") or "")
    ):
        return {"ok": True, "skipped": True, "message": "ignored Boardman's own comment"}

    if not payload.issue.pull_request:
        return await _sync_plain_issue_comment(payload, session, is_revision=is_revision)

    repo_name = payload.repository.name
    pr_number = payload.issue.number
    routing = await get_routing_async(payload.repository.full_name, repo_name, settings.github_org)
    board_id = (routing.plaky_board_id if routing and routing.plaky_board_id else "") or ""

    task_ids = await _task_ids_for_pr(session, repo_name, pr_number)
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no Plaky task linked for this PR"}

    comment_user: dict[str, Any] = {}
    commenter = ""
    comment_body = ""
    comment_edited_at = ""
    if isinstance(payload.comment, dict):
        u = payload.comment.get("user")
        if isinstance(u, dict):
            comment_user = u
            commenter = str(u.get("login") or "").strip()
        comment_body = str(payload.comment.get("body") or "")
        comment_edited_at = str(payload.comment.get("updated_at") or "").strip()

    # Same filter the plain-issue path applies, and the inline-review path claims this
    # path already had. A repo running CodeRabbit or Dependabot posts one comment per
    # finding or per bump, and every one of them was landing on the Plaky card.
    if commenter.endswith("[bot]"):
        return {"ok": True, "skipped": True, "message": "bot comment ignored"}

    bid = (board_id or "").strip()
    comment_url = (
        str(payload.comment.get("html_url") or "").strip()
        if isinstance(payload.comment, dict)
        else ""
    )
    marker = github_activity_marker(
        payload.comment,
        kind="pr-conversation-comment",
        fallback=f"{repo_name}:{pr_number}:{commenter}:{comment_body}",
    )
    comment_excerpt = comment_body[:700] + ("…" if len(comment_body) > 700 else "")
    # Plaky cannot edit a comment it already posted, so an edit arrives as its own entry.
    # Labelling it is the difference between a visible correction and an apparent duplicate.
    mirror_label = "GitHub PR comment edited" if is_revision else "GitHub PR comment"
    mirror_text = (
        f"💬 **{mirror_label}** by `{commenter or 'unknown'}` on PR #{pr_number}:\n\n"
        f"> {comment_excerpt.replace(chr(10), chr(10) + '> ')}"
    )
    if comment_url:
        mirror_text += f"\n\n{comment_url}"
    plaky = PlakyClient()
    mirrored: list[dict[str, Any]] = []
    for task_id in task_ids:
        mirrored.append(
            await mirror_github_activity(
                session,
                plaky,
                task_id=task_id,
                action="pr_comment_synced",
                marker=f"{marker}:{task_id}",
                body=mirror_text,
                board_id=bid,
                github_repo=repo_name,
                github_ref=str(pr_number),
                is_revision=is_revision,
                revision_body=comment_body,
                edited_at=comment_edited_at,
            )
        )
    await session.commit()

    if is_revision:
        # The MIRROR follows an edit; the workflow does not. A comment's instruction is
        # acted on when it is made. Re-running the state machine on every edit means
        # fixing a typo in a week-old "pausing this while we discuss" drags a finished
        # task back to Paused, and re-driving In QA / Needs QA Again from stale text is
        # the same class of mistake. The correction is on the board either way.
        return {
            "ok": True,
            "event": "pr_comment_edit_mirrored",
            "mirrored": mirrored,
            "workflow_skipped": "an edited comment updates the record, not the state",
        }

    # --- Pause: any commenter saying "pause"/"paused"/"on hold" pauses the work. ---
    from boardman.github.pr_signals import comment_mentions_qa_or_support, comment_requests_pause

    if comment_requests_pause(comment_body):
        p_key, p_val = await _resolve_status(bid, _paused_status(), "workflow_paused")
        if not p_val:
            return {
                "ok": True,
                "skipped": True,
                "message": "pause requested but no paused status resolvable",
            }
        plaky_p = plaky
        updated_p: list[dict[str, Any]] = []
        for tid in task_ids:
            res = await _update_plaky_task_status(
                tid, p_val, board_id or "", status_field_key=p_key
            )
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
        cfg_m = load_team_assignments()
        roster_logins = {
            (getattr(m, "github_login", "") or "").strip().casefold()
            for pool in (cfg_m.members, getattr(cfg_m, "fallback_members", []) or [])
            for m in pool
            if (getattr(m, "github_login", "") or "").strip()
        }
        qa_side = support | roster_logins
        commenter_is_qa_side = bool(commenter) and commenter.casefold() in qa_side
        if not commenter_is_qa_side and comment_mentions_qa_or_support(
            comment_body, support, qa_logins=roster_logins
        ):
            q_key, q_val = await _resolve_status(
                bid, _needs_qa_again_status(), "workflow_needs_qa_again", "workflow_needs_qa"
            )
            if q_val:
                plaky_q = plaky
                updated_q: list[dict[str, Any]] = []
                for tid in task_ids:
                    res = await _update_plaky_task_status(
                        tid, q_val, board_id or "", status_field_key=q_key
                    )
                    session.add(
                        SyncLog(
                            action="pr_comment_needs_qa_again",
                            github_repo=repo_name,
                            github_ref=str(pr_number),
                            plaky_task_id=tid,
                            detail=json.dumps(
                                {"commenter": commenter, "plaky_status": q_val}, default=str
                            ),
                        )
                    )
                    updated_q.append({"task_id": tid, "plaky": res})
                await session.commit()
                if updated_q:
                    await maybe_enqueue_plaky_reorder_after_task(plaky_q, task_ids[0])
                return {
                    "ok": True,
                    "updated": updated_q,
                    "status": q_val,
                    "event": "needs_qa_again",
                }

    in_qa = _in_qa_status()
    in_qa_field_key: str | None = None
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

    participants = await fetch_pr_assignees_and_reviewers_logins(
        payload.repository.full_name,
        pr_number,
    )
    participants_cf = {str(p).casefold() for p in participants} if participants else set()
    is_participant = bool(participants_cf) and commenter.casefold() in participants_cf

    cfg = load_team_assignments()
    qa_field = await resolve_qa_assignee_field_key(bid, cfg.plaky_field_qa)
    member_plaky_id: str | None = None
    if commenter:
        member_plaky_id = _reviewer_plaky_id_from_roster(cfg, commenter)
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
            assigned_ids = plaky_item_person_ids(task_info["item"], qa_field)
            if member_plaky_id in assigned_ids:
                is_assigned_qa = True
                break

    # --- Dev resuming after an actual QA verdict (approve OR request-changes): a
    # non-QA comment while the task is in either post-review state moves it to
    # Revisions In Progress (instead of In QA) — this is the PR author or another dev
    # actively addressing feedback, not QA re-engaging. Only when the status field(s)
    # are known from the board schema so current values can be read & compared. ---
    if not is_assigned_qa:
        rej_key, rej_val = await _resolve_status(
            bid, _qa_rejected_status(), "github_pr_review_changes_requested"
        )
        appr_key, appr_val = await _resolve_status(bid, _qa_approved_status(), "github_pr_review_approved")
        verdict_checks: dict[str, set[str]] = {}
        if rej_key and rej_val:
            verdict_checks.setdefault(rej_key, set()).add(str(rej_val))
        if appr_key and appr_val:
            verdict_checks.setdefault(appr_key, set()).add(str(appr_val))
        if verdict_checks:
            ip_key, ip_val = await _resolve_status(
                bid, _in_progress_status(), "workflow_in_progress"
            )
            if ip_val:
                resumed: list[dict[str, Any]] = []
                for tid in task_ids:
                    info = await plaky.get_board_item_public(board_id or "", tid)
                    if not info.get("ok") or not info.get("item"):
                        continue
                    hit = any(
                        plaky_item_status_id(info["item"], key) == vid
                        for key, ids in verdict_checks.items()
                        for vid in ids
                    )
                    if hit:
                        res = await _update_plaky_task_status(
                            tid, ip_val, board_id or "", status_field_key=ip_key
                        )
                        session.add(
                            SyncLog(
                                action="pr_comment_resumed_in_progress",
                                github_repo=repo_name,
                                github_ref=str(pr_number),
                                plaky_task_id=tid,
                                detail=json.dumps(
                                    {"commenter": commenter, "to_status": ip_val}, default=str
                                ),
                            )
                        )
                        resumed.append({"task_id": tid, "plaky": res})
                if resumed:
                    await session.commit()
                    await maybe_enqueue_plaky_reorder_after_task(plaky, resumed[0]["task_id"])
                    return {
                        "ok": True,
                        "updated": resumed,
                        "status": ip_val,
                        "event": "revisions_in_progress",
                    }

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
    return {
        "ok": True,
        "updated": updated,
        "status": in_qa,
        "mirrored": mirrored,
    }
