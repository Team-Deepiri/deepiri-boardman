"""PR handling for GitHub webhooks: opened, merged, reviews, etc."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.assignment.config import load_team_assignments
from boardman.database.models import IssueTaskMap, PullRequestTaskLink, SyncLog
from boardman.github.webhooks import PullRequestEventPayload, PullRequestReviewCommentEventPayload
from boardman.plaky.board_schema import plaky_item_person_ids, plaky_item_status_id
from boardman.plaky.client import PlakyClient
from boardman.services.comment_dedupe import github_activity_marker, mirror_github_activity
from boardman.services.issue_handler import (
    explicit_issue_numbers,
    find_plaky_task_by_issue,
    get_linked_issue_numbers,
    linked_issue_numbers_for_pr,
)
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
from boardman.services.pr_tracker import remove_pr_row, upsert_pr_row
from boardman.services.sync_state import resolve_pr_state, status_intent_would_regress
from boardman.services.task_mutations import UpdateTaskInput, update_task_internal
from boardman.services.webhook_side_effects import maybe_enqueue_plaky_reorder_after_task
from boardman.settings import settings

_log = logging.getLogger(__name__)


async def _update_plaky_task_status(
    task_id: str,
    status_value: str,
    board_id: str,
    *,
    status_field_key: str | None = None,
) -> dict:
    """Apply status via the same path as PATCH /tasks (schema-aware field patch + legacy /tasks fallback)."""
    return await update_task_internal(
        task_id,
        UpdateTaskInput(
            status=status_value,
            plaky_board_id=board_id or None,
            status_plaky_field_key=status_field_key,
            diff_only=True,
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
    pr_state = resolve_pr_state(
        pull_request,
        repo_full_name=repo_full,
        repo_name=repo_full.rsplit("/", 1)[-1],
    )
    if canon_type:
        res = await update_task_internal(
            task_id,
            UpdateTaskInput(
                task_type=canon_type,
                # Priority set on the ISSUE is the human's call. A PR carrying no
                # priority label has no opinion, and sending its guess reset tasks
                # marked VERY IMPORTANT back to Medium the moment a fix was opened.
                priority=pr_state.priority if pr_state.priority_explicit else None,
                plaky_board_id=bid or None,
                diff_only=True,
            ),
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
    # Decide eligibility BEFORE resolving a status: writing "Assigned" and then having
    # the developer refused downstream leaves the board saying Assigned with an empty
    # Assignee column, which is the one state the workflow rules forbid.
    from boardman.assignment.developer_eligibility import filter_developer

    plaky_id, refusal = filter_developer(plaky_id)
    if not plaky_id:
        out["assignee"] = {
            "skipped": "not_a_developer",
            "login": author.get("login"),
            "reason": refusal,
        }
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
    # "filled" reports the WRITE, not the intent. Hardcoding True logged and returned a
    # developer assignment that the column never received.
    refused = res.get("developer_not_assigned") or ""
    out["assignee"] = {
        "filled": bool(res.get("ok")) and not refused,
        "plaky_id": plaky_id,
        "login": author.get("login"),
        "status": assigned_status_val or None,
        "ok": res.get("ok"),
    }
    if refused:
        out["assignee"]["reason"] = refused
    return out


def _member_by_name(cfg: Any, name: str) -> Any | None:
    """Resolve a policy role (e.g. the bug specialist) by display name or GitHub login.

    Checks the live roster first, then the yaml fallback list — the specialist may not be
    on the GitHub support team the live roster is built from (Hameeda is exactly this
    case), and a policy the employer stated must not silently stop applying because of
    team-membership drift.
    """
    want = (name or "").strip().casefold()
    if not want:
        return None
    for pool in (cfg.members, getattr(cfg, "fallback_members", []) or []):
        for m in pool:
            display = (getattr(m, "display", "") or "").strip().casefold()
            login = (getattr(m, "github_login", "") or "").strip().casefold()
            if want in (display, login):
                return m
    return None


async def _task_type_is_bug(plaky: PlakyClient, board_id: str, task_id: str) -> bool:
    """Read the task's CURRENT Type off the board and compare to 'Bug'.

    The board value is the merged truth: label-derived at issue creation, possibly
    overwritten from the PR branch just before QA assignment runs. Inferring from the PR
    alone would miss a bug-labeled issue fixed from a feature/ branch."""
    from boardman.plaky.board_schema import fetch_board_schema_bundle, plaky_item_status_id

    bid = (board_id or "").strip()
    if not bid:
        return False
    bundle = await fetch_board_schema_bundle(bid)
    fields = ((bundle.get("normalized") or {}) if isinstance(bundle, dict) else {}).get(
        "fields"
    ) or []
    row = next(
        (
            f
            for f in fields
            if isinstance(f, dict)
            and "type" in str(f.get("name") or "").casefold()
            and f.get("options")
        ),
        None,
    )
    if not row:
        return False
    info = await plaky.get_board_item_public(bid, task_id)
    item = info.get("item") if isinstance(info, dict) else None
    if not item:
        return False
    val = str(plaky_item_status_id(item, str(row.get("key") or "")) or "")
    label = next(
        (
            str(o.get("name") or o.get("title") or "")
            for o in row["options"]
            if isinstance(o, dict) and str(o.get("id") or o.get("key") or "") == val
        ),
        "",
    )
    return label.strip().casefold() == "bug"


async def _assign_qa_for_pr(
    plaky: PlakyClient,
    *,
    task_id: str,
    board_id: str,
    repo_full: str,
    pr_number: int,
    pr_author_login: str = "",
    task_url: str = "",
) -> dict[str, Any]:
    """QA assignment happens HERE — when a PR opens — not at task creation (employer flow).

    Pick via the GitHub-fit algorithm (exclusion list applied), @mention the QA on the PR,
    request them as reviewer, and write their Plaky profile into the task's QA field.
    Never overwrites an already-assigned QA.
    """
    from boardman.assignment.qa_picker import pick_qa_for_repo as _pick
    from boardman.github.pr_actions import comment_on_pr, request_reviewers
    from boardman.plaky.dynamic_qa_status import resolve_qa_assignee_field_key

    out: dict[str, Any] = {}
    bid = (board_id or "").strip()
    cfg = load_team_assignments()
    qa_key = (
        await resolve_qa_assignee_field_key(bid, cfg.plaky_field_qa)
        if bid
        else (cfg.plaky_field_qa or "")
    )
    if not qa_key:
        return {"skipped": "no QA field key resolvable for this board"}

    if bid:
        current_qa = await _current_person_field_value(plaky, bid, task_id, qa_key)
        if current_qa:
            return {"skipped": "qa_already_assigned", "qa_plaky_id": current_qa}

    # Bug-typed tasks always go to the QA bug specialist (employer: "bug - assign to
    # Hameeda") - unless she authored the PR (self-review) or the role is unset/unresolvable,
    # in which case the ranked pick applies as usual.
    qid: str | None = None
    why = ""
    specialist_name = (getattr(cfg, "qa_bug_specialist", "") or "").strip()
    if specialist_name and await _task_type_is_bug(plaky, bid, task_id):
        sm = _member_by_name(cfg, specialist_name)
        if sm is None:
            _log.warning(
                "qa_bug_specialist %r not in roster or fallback - using ranked pick",
                specialist_name,
            )
        elif (getattr(sm, "github_login", "") or "").casefold() == (
            pr_author_login or ""
        ).casefold() and pr_author_login:
            _log.info("qa_bug_specialist authored PR #%s - using ranked pick", pr_number)
        else:
            qid = str(sm.id)
            why = f"bug task -> QA bug specialist {getattr(sm, 'display', specialist_name)}"

    if not qid:
        # The author is never a candidate (self-review). GitHub refuses a review from
        # the PR's own author, so assigning them leaves the task with a reviewer who
        # cannot act, and the QA-gated rejection path stops responding to anyone.
        qid, why = await _pick(repo_full, exclude_login=pr_author_login)
    if not qid:
        return {"skipped": "no eligible QA", "reason": why}

    member = next((m for m in cfg.members if (m.id or "").strip() == str(qid)), None)
    qa_login = (getattr(member, "github_login", "") or "").strip() if member else ""
    qa_display = (getattr(member, "display", "") or "").strip() if member else ""

    res = await update_task_internal(
        task_id,
        UpdateTaskInput(qa_plaky_id=str(qid), plaky_board_id=bid or None),
    )
    out["plaky_qa"] = {
        "id": str(qid),
        "display": qa_display,
        "ok": res.get("ok"),
        "reason": why[:220],
    }

    mention = f"@{qa_login}" if qa_login else (qa_display or "QA")
    task_ref = task_url or f"Plaky task `{task_id}`"
    body = (
        f"{mention} you've been assigned as **QA reviewer** for this PR by Boardman.\n\n"
        f"Linked task: {task_ref}\n"
        f"Why you: {why[:300]}"
    )
    out["github_comment"] = await comment_on_pr(repo_full, pr_number, body)
    if qa_login and qa_login.casefold() != (pr_author_login or "").casefold():
        out["github_reviewer"] = await request_reviewers(repo_full, pr_number, [qa_login])
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
    await _update_plaky_task_status(task_id, st, bid, status_field_key=status_field_key)
    await maybe_enqueue_plaky_reorder_after_task(plaky, task_id)


async def _maybe_triage_ambiguous_pr(
    payload: PullRequestEventPayload,
    session: AsyncSession,
    top_scored: Sequence[Any] | None = None,
    orphan_issue_number: int = 0,
) -> dict[str, Any] | None:
    """A PR that matches no existing task gets a REAL task, not a triage stub.

    Employer flow: "if a plaky task already exists for a pr then it connects to that
    pr, else it makes a new one." The old triage stub had no PullRequestTaskLink, so
    merge/review/synchronize events could never reach it - a dead card. The created
    task is a first-class citizen: titled after the PR, typed from branch/labels,
    assignee = the PR author (they are the one writing the code), status Needs QA
    (an open non-draft PR IS ready for review), linked, and QA-assigned.

    Board/group: explicit ambiguous_pr.triage_* ids win; otherwise the repo's normal
    routing. Idempotent per PR - reopen/edit must not create a second task.
    """
    cfg = load_team_assignments()
    amb = cfg.ambiguous_pr
    if not amb.enabled:
        return None

    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    pr_url = payload.pull_request.html_url
    full_name = payload.repository.full_name

    # Never manufacture review work for a pull request that already shipped. A closed
    # or merged PR replayed through this path (reconciliation sweep, redelivered
    # webhook, poller catch-up) would create a task, assign a QA and park it at
    # Needs QA for code that merged long ago.
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
    bid = (amb.triage_board_id or "").strip() or (
        (routing.plaky_board_id if routing and routing.plaky_board_id else "") or ""
    ).strip()
    gid = (amb.triage_group_id or "").strip() or (
        (routing.plaky_group_id if routing and routing.plaky_group_id else "") or ""
    ).strip()
    if not bid:
        return {
            "ok": True,
            "skipped": True,
            "message": "no board resolvable for orphan-PR task (routing and triage ids empty)",
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
        link_source="pr_task_pending",
    )
    try:
        async with session.begin_nested():
            session.add(reservation)
            await session.flush()
    except IntegrityError:
        linked = await distinct_task_ids_for_pr(
            session, github_repo=repo_name, github_pr_number=pr_number
        )
        if linked:
            return {
                "ok": True,
                "skipped": True,
                "message": "task already created for this PR",
                "ambiguous_triage": True,
            }
        raise

    from boardman.github.pr_signals import infer_task_type_from_pr, pr_label_names
    from boardman.plaky.dynamic_qa_status import (
        github_actor_payload,
        resolve_github_user_to_plaky_user_id,
    )
    from boardman.services.priority_rules import infer_priority_from_text
    from boardman.services.task_mutations import CreateTaskInput, create_task_internal

    pr_obj = payload.pull_request
    head = getattr(pr_obj, "head", None)
    head_ref = str(head.get("ref") or "") if isinstance(head, dict) else ""
    labels = pr_label_names(getattr(pr_obj, "labels", None))
    task_type = infer_task_type_from_pr(head_ref, labels) or "Feature"
    is_draft = bool(getattr(pr_obj, "draft", False))

    pr_user = getattr(pr_obj, "user", None)
    author_login = ""
    author_plaky = ""
    if isinstance(pr_user, dict):
        author_login = str(pr_user.get("login") or "").strip()
        author_plaky = (
            await resolve_github_user_to_plaky_user_id(github_actor_payload(pr_user)) or ""
        )

    title = str(getattr(pr_obj, "title", "") or "").strip() or amb.title_template.format(
        number=pr_number, repo=repo_name, full_name=full_name
    )
    body_text = str(getattr(pr_obj, "body", "") or "")
    priority = infer_priority_from_text(title, body_text, labels)
    description = (
        f"Auto-created from GitHub PR (no existing task matched): {pr_url}\n\n"
        f"**Repo:** `{full_name}`  **Branch:** `{head_ref or '?'}`  "
        f"**Author:** `{author_login or 'unknown'}`\n\n"
        "The PR did not reference an issue and fuzzy matching found no confident task, "
        "so this task now represents that work.\n"
    )
    if top_scored:
        description += "\nClosest existing candidates considered:\n" + format_triage_comment(
            top_scored
        )

    res = await create_task_internal(
        CreateTaskInput(
            title=title,
            description=description,
            priority=priority,
            # Non-draft PR = the work is up for review NOW. Draft: status follows the
            # assignee (author present -> Assigned).
            status="" if is_draft else "Needs QA",
            task_type=task_type,
            repo=full_name,
            plaky_board_id=bid,
            plaky_group_id=gid or None,
            engineer_plaky_id=author_plaky or None,
            auto_assign_team=False,
        )
    )
    if not res.get("ok"):
        await session.delete(reservation)
        return {"ok": False, "message": res.get("message"), "ambiguous_triage": True}

    task_id = str(res.get("task", {}).get("id") or res.get("task", {}).get("taskId") or "")

    if task_id:
        reservation.plaky_task_id = task_id
        reservation.link_source = "pr_task_created"
        # The PR named an issue that has no task yet. Claim that issue for THIS card, so
        # when the issue itself syncs it updates this one instead of opening a second
        # card for the same piece of work.
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
        plaky = PlakyClient()
        await plaky.add_comment(
            task_id,
            format_pr_notice_with_url(headline="**PR opened:**", pr_number=pr_number, pr_url=pr_url)
            + "\n\nBoardman created this task from the PR because no existing task matched.",
            board_id=bid,
        )
        if amb.assign_qa:
            qa_res = await _assign_qa_for_pr(
                plaky,
                task_id=task_id,
                board_id=bid,
                repo_full=full_name,
                pr_number=pr_number,
                pr_author_login=author_login,
                task_url=str(res.get("task_url") or ""),
            )
        else:
            qa_res = {"skipped": "ambiguous_pr.assign_qa is false"}
    else:
        await session.delete(reservation)
        qa_res = {"skipped": "task id missing from create result"}

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
                    "assignee": author_plaky,
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
        "assignee_plaky_id": author_plaky,
        "qa": qa_res,
    }


async def _link_pr_to_issue_task(
    session: AsyncSession,
    plaky: PlakyClient,
    *,
    payload: PullRequestEventPayload,
    issue_number: int,
    mapping: Any,
    board_id: str,
    is_draft: bool,
    headline: str,
) -> None:
    """Attach one PR to the Plaky task an issue already owns, and run the PR workflow.

    One body for two callers: `opened`, and `edited` when a PR gains a closing keyword it
    did not have before. A PR that is linked late must land in the same state as one
    linked at creation -- same notice, same type/assignee, same QA assignment, same
    Needs QA gate -- or "when did you add Fixes #94" becomes a thing people have to know.
    Every step below is idempotent, so a replayed edit re-runs it without duplicating.
    """
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    pr_url = payload.pull_request.html_url

    await upsert_pr_task_link(
        session,
        github_repo=repo_name,
        github_pr_number=pr_number,
        plaky_task_id=mapping.plaky_task_id,
        github_issue_number=int(issue_number),
        link_source="issue_keyword",
    )
    marker = f"github:pr-link-notice:{repo_name}:{pr_number}:{issue_number}"
    await mirror_github_activity(
        session,
        plaky,
        task_id=mapping.plaky_task_id,
        action="pr_link_notice",
        marker=marker,
        body=format_pr_notice_with_url(headline=headline, pr_number=pr_number, pr_url=pr_url),
        board_id=board_id or "",
        github_repo=repo_name,
        github_ref=str(pr_number),
    )
    await _apply_pr_type_and_assignee(
        plaky,
        task_id=mapping.plaky_task_id,
        board_id=board_id,
        pull_request=payload.pull_request,
        repo_full=payload.repository.full_name,
    )
    pr_user0 = payload.pull_request.user or {}
    qa_res = await _assign_qa_for_pr(
        plaky,
        task_id=mapping.plaky_task_id,
        board_id=board_id,
        repo_full=payload.repository.full_name,
        pr_number=pr_number,
        pr_author_login=(str(pr_user0.get("login") or "") if isinstance(pr_user0, dict) else ""),
        task_url=mapping.plaky_task_url or "",
    )
    _log.info("PR #%s QA assignment: %s", pr_number, {k: qa_res[k] for k in list(qa_res)[:3]})
    await _maybe_set_needs_qa(plaky, mapping.plaky_task_id, is_draft, board_id)
    session.add(
        SyncLog(
            action="pr_linked",
            github_repo=repo_name,
            github_ref=str(pr_number),
            plaky_task_id=mapping.plaky_task_id,
            detail=json.dumps({"issue_number": issue_number, "pr_url": pr_url}),
        )
    )


async def handle_pr_opened(payload: PullRequestEventPayload, session: AsyncSession) -> dict:
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    pr_url = payload.pull_request.html_url
    is_draft = bool(payload.pull_request.draft)
    full_name = payload.repository.full_name

    # Title and branch too, exactly as `edited` reads them. Reading only the body here
    # meant a PR whose keyword lives in its title got a duplicate standalone task at open
    # and was reconciled only when somebody happened to edit it.
    opened_state = resolve_pr_state(
        payload.pull_request, repo_full_name=full_name, repo_name=repo_name
    )
    linked_issues = linked_issue_numbers_for_pr(
        body=payload.pull_request.body,
        title=payload.pull_request.title,
        head_ref=opened_state.head_ref,
    )

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
            await _link_pr_to_issue_task(
                session,
                plaky,
                payload=payload,
                issue_number=int(issue_num),
                mapping=mapping,
                board_id=board_id,
                is_draft=is_draft,
                headline="**PR Opened:**",
            )
            results.append({"issue": issue_num, "task_id": mapping.plaky_task_id})

    # Gate on whether anything was actually LINKED, not on whether the body named an
    # issue: a PR saying 'Fixes #12' where #12 has no Plaky task used to fall through
    # both branches and end up with no task and no link row at all.
    if not results:
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
                        "triage_comment": (
                            format_triage_comment(pipe.top_scored)
                            if pipe.decision == "triage"
                            else None
                        ),
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
                qa_res2 = await _assign_qa_for_pr(
                    plaky,
                    task_id=pipe.task_id,
                    board_id=board_id,
                    repo_full=full_name,
                    pr_number=pr_number,
                    pr_author_login=str(pr_author_login or ""),
                )
                _log.info(
                    "PR #%s QA assignment (fuzzy link): %s",
                    pr_number,
                    {k: qa_res2[k] for k in list(qa_res2)[:3]},
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

        triage = await _maybe_triage_ambiguous_pr(
            payload,
            session,
            top_scored=pipe_top,
            orphan_issue_number=int(linked_issues[0]) if linked_issues else 0,
        )
        if triage is not None:
            return triage
        message = (
            f"named issue(s) {sorted(linked_issues)} but none has a Plaky task"
            if linked_issues
            else "No linked issues found"
        )
        return {"ok": True, "skipped": True, "message": message}

    await session.commit()
    return {"ok": True, "linked": results}


async def reconcile_pr_issue_links(
    payload: PullRequestEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    """Re-resolve a PR's explicit issue relationships against its canonical link rows.

    The bug this exists for: a PR opened without `Fixes #94` and edited later to add it
    stayed unlinked forever, because `edited` treated any already-linked PR as
    metadata-only. GitHub is authoritative for the relationship, and the author states it
    whenever they state it -- at open or three days later -- so every `edited` re-reads it.

    Rules, in order:
      * nothing explicit written -> change nothing. A fuzzy or human-curated link stands.
      * the same issues as the existing links -> change nothing. Repeated deliveries of
        the same edit are therefore no-ops, which is what makes this idempotent.
      * a newly named issue that already owns a Plaky task -> link the PR to THAT task and
        run the same workflow `opened` runs. Never create a second task for it.
      * an issue that is no longer named -> withdraw that link, but only once a new
        reference has actually resolved. A body edited to name nothing, or to name an
        issue with no task yet, must not tear down a working mapping.
    """
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    state = resolve_pr_state(
        payload.pull_request,
        repo_full_name=payload.repository.full_name,
        repo_name=repo_name,
    )
    written = explicit_issue_numbers(payload.pull_request.body, payload.pull_request.title)
    referenced = linked_issue_numbers_for_pr(
        body=payload.pull_request.body,
        title=payload.pull_request.title,
        head_ref=state.head_ref,
    )
    if not referenced:
        return {"ok": True, "changed": False, "reason": "no explicit issue reference"}

    all_rows = list(
        (
            await session.execute(
                select(PullRequestTaskLink).where(
                    PullRequestTaskLink.github_repo == repo_name,
                    PullRequestTaskLink.github_pr_number == pr_number,
                    PullRequestTaskLink.withdrawn_at.is_(None),
                )
            )
        ).scalars()
    )
    rows = [row for row in all_rows if int(row.github_issue_number) != 0]
    # issue_number=0 is the fallback link to a task this PR got on its own, because at the
    # time nobody knew which issue it belonged to. Once the author says which issue it
    # closes, that guess is superseded -- leaving it live would send every comment and
    # status change to two cards for one piece of work.
    standalone_rows = [row for row in all_rows if int(row.github_issue_number) == 0]
    existing = {int(row.github_issue_number) for row in rows}
    if existing == set(referenced):
        return {"ok": True, "changed": False, "reason": "issue relationships unchanged"}

    from boardman.repos_config import get_routing_async

    routing = await get_routing_async(payload.repository.full_name, repo_name, settings.github_org)
    board_id = (routing.plaky_board_id if routing and routing.plaky_board_id else "") or ""
    plaky = PlakyClient()
    is_draft = bool(payload.pull_request.draft)

    if not written and (rows or standalone_rows):
        # Branch-only, and this PR already has a live link. The branch may introduce a
        # relationship where there is none; it may not add a second card beside one that
        # exists, and it may not retire it either.
        return {
            "ok": True,
            "changed": False,
            "reason": "branch-only reference; the existing link stands",
            "referenced": referenced,
        }

    linked: list[dict[str, Any]] = []
    unresolved: list[int] = []
    for issue_num in referenced:
        mapping = await find_plaky_task_by_issue(repo_name, int(issue_num), session)
        if not mapping or not mapping.plaky_task_id:
            unresolved.append(int(issue_num))
            continue
        if int(issue_num) in existing:
            continue  # already the canonical link; nothing to re-run
        await _link_pr_to_issue_task(
            session,
            plaky,
            payload=payload,
            issue_number=int(issue_num),
            mapping=mapping,
            board_id=board_id,
            is_draft=is_draft,
            headline="**PR Linked:**",
        )
        linked.append({"issue": int(issue_num), "task_id": mapping.plaky_task_id})

    withdrawn: list[dict[str, Any]] = []
    # A branch name is a convention, not a statement. It may introduce a link where there
    # was none, but it must never be the reason a curated one is torn down: a PR on
    # `94-add-retries` whose body loses `Fixes #95` would otherwise silently move to
    # issue 94's task and withdraw the working 95 link.
    if linked and written:
        # Only now: the author WROTE a different issue, and that issue has a task.
        now = datetime.utcnow()
        superseded = [row for row in rows if int(row.github_issue_number) not in referenced]
        superseded += standalone_rows
        for row in superseded:
            issue_number = int(row.github_issue_number)
            row.withdrawn_at = now
            withdrawn.append({"issue": issue_number, "task_id": str(row.plaky_task_id)})
            session.add(
                SyncLog(
                    action="pr_link_withdrawn",
                    github_repo=repo_name,
                    github_ref=str(pr_number),
                    plaky_task_id=str(row.plaky_task_id),
                    detail=json.dumps(
                        {
                            "issue_number": issue_number,
                            "reason": (
                                "PR gained an explicit issue link; its standalone task is "
                                "superseded"
                                if issue_number == 0
                                else "PR now references a different issue"
                            ),
                            "now_references": referenced,
                        },
                        default=str,
                    ),
                )
            )

    if linked or withdrawn:
        await session.commit()
    return {
        "ok": True,
        "changed": bool(linked or withdrawn),
        "referenced": referenced,
        "linked": linked,
        "withdrawn": withdrawn,
        "unresolved_issues": unresolved,
    }


async def handle_pr_edited(
    payload: PullRequestEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    """PR metadata edited: re-resolve issue relationships, then sync metadata.

    A PR opened without `Fixes #N` that is later edited to include one links to that
    issue's existing Plaky task (see `reconcile_pr_issue_links`). A PR that names no
    issue at all and has no task falls through to the full opened pipeline.
    """
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    state = (payload.pull_request.state or "").strip().casefold()
    if state and state != "open":
        return {"ok": True, "skipped": True, "message": "PR not open; edit ignored"}

    relink = await reconcile_pr_issue_links(payload, session)

    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if task_ids:
        from boardman.repos_config import get_routing_async

        routing = await get_routing_async(
            payload.repository.full_name, repo_name, settings.github_org
        )
        board_id = str(getattr(routing, "plaky_board_id", "") or "").strip()
        state = resolve_pr_state(
            payload.pull_request,
            repo_full_name=payload.repository.full_name,
            repo_name=repo_name,
        )
        from boardman.plaky.dynamic_qa_status import (
            github_actor_payload,
            resolve_github_user_to_plaky_user_id,
            resolve_plaky_status_patch,
        )

        engineer_id = ""
        if state.assignee_login:
            engineer_id = str(
                await resolve_github_user_to_plaky_user_id(
                    github_actor_payload({"login": state.assignee_login})
                )
                or ""
            ).strip()
        status_value = ""
        status_key: str | None = None
        if board_id and state.draft:
            resolved = await resolve_plaky_status_patch(board_id, intent="workflow_assigned")
            if resolved:
                status_key, status_value = resolved[0], resolved[1]
        standalone_ids = {
            str(row.plaky_task_id)
            for row in (
                await session.execute(
                    select(PullRequestTaskLink).where(
                        PullRequestTaskLink.github_repo == repo_name,
                        PullRequestTaskLink.github_pr_number == pr_number,
                        PullRequestTaskLink.github_issue_number == 0,
                    )
                )
            ).scalars()
            if str(row.plaky_task_id).strip()
        }
        standalone_description = (
            f"Auto-created from GitHub PR: {state.url}\n\n"
            f"**Repo:** `{state.repo_full_name}`  **Branch:** `{state.head_ref or '?'}`  "
            f"**Author:** `{state.author_login or 'unknown'}`\n\n{state.body}"
        )
        plaky_results: list[dict[str, Any]] = []
        for task_id in task_ids:
            # Per task, because "how far along is it" is a property of the task, not of
            # the PR. A draft PR resolves to Assigned, and writing that over a task QA is
            # already reviewing loses QA's position -- the same mistake the issue path
            # made on `assigned`. Deliberate transitions do not come through here.
            task_status, task_status_key = status_value, status_key
            if task_status:
                from boardman.plaky.dynamic_qa_status import current_status_intent

                now_at = await current_status_intent(board_id, task_id, status_key or "")
                if status_intent_would_regress(now_at, "workflow_assigned"):
                    _log.info(
                        "PR #%s: keeping task %s at %s; Assigned would move it backwards",
                        pr_number,
                        task_id,
                        now_at,
                    )
                    task_status, task_status_key = "", None
            mutation = await update_task_internal(
                task_id,
                UpdateTaskInput(
                    title=(state.title or None) if task_id in standalone_ids else None,
                    description=standalone_description if task_id in standalone_ids else None,
                    task_type=state.task_type,
                    priority=state.priority if state.priority_explicit else None,
                    engineer_plaky_id=engineer_id or None,
                    plaky_board_id=board_id or None,
                    status=task_status or None,
                    status_plaky_field_key=task_status_key,
                    diff_only=True,
                ),
            )
            plaky_results.append({"task_id": task_id, "mutation": mutation})
            session.add(
                SyncLog(
                    action="pr_metadata_synced",
                    github_repo=repo_name,
                    github_ref=str(pr_number),
                    plaky_task_id=task_id,
                    detail=json.dumps(
                        {
                            "event": payload.action,
                            "task_type": state.task_type,
                            "priority": state.priority,
                            "assignee_login": state.assignee_login,
                            "status": task_status,
                            "plaky_ok": mutation.get("ok"),
                        },
                        default=str,
                    ),
                )
            )
        await session.commit()
        all_failed = bool(plaky_results) and not any(x["mutation"].get("ok") for x in plaky_results)
        return {
            "ok": not all_failed,
            "skipped": all_failed,
            "event": "pr_metadata_synced",
            "updated": plaky_results,
            "relink": relink,
        }
    opened = await handle_pr_opened(payload, session)
    if isinstance(opened, dict):
        opened.setdefault("relink", relink)
    return opened


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

    request_removed = payload.action == "review_request_removed"
    target_status = (
        (settings.plaky_pr_needs_qa_status or settings.plaky_status_needs_qa or "").strip()
        if request_removed
        else (settings.plaky_pr_in_qa_status or settings.plaky_status_in_qa or "").strip()
    )
    target_field_key: str | None = None
    bid = (board_id or "").strip()
    if not target_status and bid:
        from boardman.plaky.dynamic_qa_status import resolve_plaky_status_patch

        rp = await resolve_plaky_status_patch(
            bid,
            intent="workflow_needs_qa" if request_removed else "workflow_in_qa",
        )
        if rp:
            target_field_key, target_status = rp[0], rp[1]
    if not target_status:
        return {
            "ok": True,
            "skipped": True,
            "message": "review status not configured or discoverable",
        }

    plaky = PlakyClient()
    for tid in task_ids:
        await _update_plaky_task_status(
            tid, target_status, board_id or "", status_field_key=target_field_key
        )
    await session.commit()
    if task_ids:
        await maybe_enqueue_plaky_reorder_after_task(plaky, task_ids[0])
    return {
        "ok": True,
        "tasks": task_ids,
        "status": target_status,
        "event": "review_request_removed" if request_removed else "review_requested",
    }


async def handle_pr_synchronized(
    payload: PullRequestEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    """New commits pushed (pull_request.synchronize): if a linked task is currently
    QA-rejected, the developer has addressed the review → the work is RESUBMITTED, not
    merely resumed. Employer: "developer made these changes so it needs QA again."

    Needing QA again just means going back to Needs QA (Ali, 2026-08-19) — that is the
    intended destination, not a fallback. A board that happens to define a dedicated
    "…Again" column gets it; the Bots board does not, and Needs QA is correct there.
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
    approved = await resolve_plaky_status_patch(bid, intent="github_pr_review_approved")
    target = await resolve_plaky_status_patch(bid, intent="workflow_needs_qa_again")
    if not target:
        target = await resolve_plaky_status_patch(bid, intent="workflow_needs_qa")
    if not rejected or not target:
        return {
            "ok": True,
            "skipped": True,
            "message": "qa-rejected / needs-qa status not resolvable from board",
        }
    rej_key, rej_id = rejected
    ip_key, ip_id = target
    # Each verdict is checked under ITS OWN field key. On every current board both
    # verdicts live in the one Status column, but if a board ever splits them, reading
    # only the rejected key would miss a QA Verified task (or falsely match an id
    # collision). Keys are deduped so the common case stays a single read.
    stale_checks: dict[str, set[str]] = {rej_key: {str(rej_id)}}
    if approved:
        stale_checks.setdefault(approved[0], set()).add(str(approved[1]))

    plaky = PlakyClient()
    resumed: list[dict[str, Any]] = []
    for tid in task_ids:
        stale_hit = False
        for check_key, stale_ids in stale_checks.items():
            current = await _current_status_value(plaky, bid, tid, check_key)
            if current and current in stale_ids:
                stale_hit = True
                break
        if not stale_hit:
            continue
        res = await _update_plaky_task_status(tid, ip_id, bid, status_field_key=ip_key)
        session.add(
            SyncLog(
                action="pr_resubmitted_needs_qa_again",
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
    return {"ok": True, "updated": resumed, "event": "resubmitted_needs_qa_again"}


async def handle_pr_closed_without_merge(
    payload: PullRequestEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    """Withdraw the link; if that was the task's LAST open PR, review is over.

    A task parked at Needs QA / In QA with zero open PRs is a lie on the board — there
    is nothing left to review. Revert those (and only those) to In Progress; verdicts
    like QA Verified/Rejected and terminal states are left alone.
    """
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    rows = await mark_pr_withdrawn(session, github_repo=repo_name, github_pr_number=pr_number)

    reverted: list[dict[str, Any]] = []
    if task_ids:
        from boardman.repos_config import get_routing_async

        routing = await get_routing_async(
            payload.repository.full_name, repo_name, settings.github_org
        )
        bid = ((routing.plaky_board_id if routing and routing.plaky_board_id else "") or "").strip()
        if bid:
            from boardman.plaky.dynamic_qa_status import resolve_plaky_status_patch

            in_progress = await resolve_plaky_status_patch(bid, intent="workflow_in_progress")
            review_vals: set[str] = set()
            for intent in ("workflow_needs_qa", "workflow_needs_qa_again", "workflow_in_qa"):
                rp = await resolve_plaky_status_patch(bid, intent=intent)
                if rp:
                    review_vals.add(str(rp[1]))
            if in_progress and review_vals:
                ip_key, ip_val = in_progress
                plaky = PlakyClient()
                for tid in task_ids:
                    if await has_any_open_pr_for_task(session, plaky_task_id=tid):
                        continue
                    current = await _current_status_value(plaky, bid, tid, ip_key)
                    if current not in review_vals:
                        continue
                    res = await _update_plaky_task_status(tid, ip_val, bid, status_field_key=ip_key)
                    reverted.append({"task_id": tid, "plaky": res})

    log = SyncLog(
        action="pr_closed_without_merge",
        github_repo=repo_name,
        github_ref=str(pr_number),
        plaky_task_id=task_ids[0] if task_ids else None,
        detail=json.dumps({"withdrawn_links": len(rows), "reverted": len(reverted)}),
    )
    session.add(log)
    await session.commit()
    return {"ok": True, "withdrawn_links": len(rows), "reverted": reverted}


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

    merge_routing = await get_routing_async(
        payload.repository.full_name, repo_name, settings.github_org
    )
    board_id_merge = (
        merge_routing.plaky_board_id if merge_routing and merge_routing.plaky_board_id else ""
    ) or ""

    # Merged → Completed. Explicit env wins; otherwise resolve "Completed" from the live board.
    merge_status = (settings.plaky_pr_merge_status or "").strip()
    merge_status_field_key: str | None = None
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


async def handle_pr_review_comment(
    payload: PullRequestReviewCommentEventPayload, session: AsyncSession
) -> dict:
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

    linked_issues = await get_linked_issue_numbers(
        payload.pull_request.body if payload.pull_request else None
    )

    task_ids_with_issue: list[tuple[str, int | None]] = []
    for issue_num in linked_issues:
        mapping = await find_plaky_task_by_issue(repo_name, issue_num, session)
        if mapping:
            task_ids_with_issue.append((mapping.plaky_task_id, int(issue_num)))

    if not task_ids_with_issue:
        from boardman.services.pr_task_registry import distinct_task_ids_for_pr

        for tid in await distinct_task_ids_for_pr(
            session, github_repo=repo_name, github_pr_number=pr_number
        ):
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

    plaky = PlakyClient()
    comment_body = str(comment.get("body") or "").strip()
    comment_marker = github_activity_marker(
        comment,
        kind="pr-review-comment",
        fallback=f"{repo_name}:{pr_number}:{commenter_login}:{comment_body}",
    )
    comment_url = str(comment.get("html_url") or "").strip()
    # Same rule as conversation comments: Plaky cannot edit a posted comment, so an edit
    # is its own labelled entry, deduped on (comment id, GitHub text). Without this an
    # edited inline review comment matched the plain marker and vanished silently.
    is_revision = str(getattr(payload, "action", "") or "") == "edited"
    review_label = (
        "GitHub inline review comment edited" if is_revision else "GitHub inline review comment"
    )
    comment_text = (
        f"💬 **{review_label}** by `{commenter_login}` on PR #{pr_number}:\n\n"
        f"> {comment_body[:1000].replace(chr(10), chr(10) + '> ')}"
    )
    if comment_url:
        comment_text += f"\n\n{comment_url}"
    mirrored: list[dict[str, Any]] = []
    for task_id, _issue_num in task_ids_with_issue:
        mirrored.append(
            await mirror_github_activity(
                session,
                plaky,
                task_id=task_id,
                action="pr_review_comment_synced",
                marker=f"{comment_marker}:{task_id}",
                body=comment_text,
                board_id=board_id,
                github_repo=repo_name,
                github_ref=str(pr_number),
                is_revision=is_revision,
                revision_body=comment_body,
            )
        )
    await session.commit()

    if is_revision:
        # Mirror only. Fixing a typo in an old inline review comment must not drag a
        # QA-Verified or Completed task back to In QA; the comment's instruction was
        # acted on when it was made. Same rule as handle_issue_comment_on_pr.
        return {
            "ok": True,
            "event": "pr_review_comment_edit_mirrored",
            "mirrored": mirrored,
            "workflow_skipped": "an edited comment updates the record, not the state",
        }

    qa_field = await resolve_qa_assignee_field_key(board_id, cfg.plaky_field_qa)
    if not qa_field:
        return {
            "ok": True,
            "skipped": True,
            "message": "QA field not configured or discoverable on board",
        }

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
            status_to_set = (
                settings.plaky_pr_in_qa_status or settings.plaky_status_in_qa or ""
            ).strip()
            if not status_to_set and board_id:
                rp = await resolve_plaky_status_patch(board_id, intent="workflow_in_qa")
                if rp:
                    status_field_key, status_to_set = rp[0], rp[1]
            if not status_to_set:
                continue
            await _update_plaky_task_status(
                task_id, status_to_set, board_id, status_field_key=status_field_key
            )
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
    return {"ok": True, "updated": results, "mirrored": mirrored}


async def handle_pr_labels_changed(
    payload: PullRequestEventPayload,
    session: AsyncSession,
) -> dict[str, Any]:
    """PR `labeled`/`unlabeled` → re-mirror Type onto the linked task(s).

    PRs have no native GitHub "type" — labels ARE their typing (meeting note: "match
    labels as well; PRs don't have types, only issues do"). Same shape as the issue-side
    label sync: labeling after the fact is normal usage, and the creation-time race must
    not freeze the Type forever. Type only — assignee/QA/status are owned by their own
    transitions.
    """
    from boardman.github.pr_signals import infer_task_type_from_pr, pr_label_names
    from boardman.repos_config import get_routing_async

    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    task_ids = await distinct_task_ids_for_pr(
        session, github_repo=repo_name, github_pr_number=pr_number
    )
    if not task_ids:
        return {"ok": True, "skipped": True, "message": "no Plaky task linked for this PR"}

    # Labels ONLY — no branch fallback. The branch already set Type at link time; the
    # event firing here is a human changing the labels, and if the branch kept winning
    # ("feature/x" beats a freshly added "bug" label) this handler could never change
    # anything, which is exactly the frozen-Type problem it exists to fix.
    labels = pr_label_names(getattr(payload.pull_request, "labels", None))
    label_type = infer_task_type_from_pr(None, labels)
    removed_type = infer_task_type_from_pr(
        None,
        (
            pr_label_names([getattr(payload, "label", None)])
            if getattr(payload, "label", None)
            else []
        ),
    )
    if not label_type and not (payload.action == "unlabeled" and removed_type):
        return {"ok": True, "skipped": True, "message": "labels carry no type signal"}
    canon_type = label_type or "Feature"
    pr_state = resolve_pr_state(
        payload.pull_request,
        repo_full_name=payload.repository.full_name,
        repo_name=repo_name,
    )

    routing = await get_routing_async(payload.repository.full_name, repo_name, settings.github_org)
    bid = ((routing.plaky_board_id if routing and routing.plaky_board_id else "") or "").strip()

    updated: list[dict[str, Any]] = []
    for tid in task_ids:
        res = await update_task_internal(
            tid,
            UpdateTaskInput(
                task_type=canon_type,
                priority=pr_state.priority if pr_state.priority_explicit else None,
                plaky_board_id=bid or None,
                diff_only=True,
            ),
        )
        updated.append({"task_id": tid, "ok": res.get("ok")})
    session.add(
        SyncLog(
            action="pr_labels_synced",
            github_repo=repo_name,
            github_ref=str(pr_number),
            plaky_task_id=task_ids[0],
            detail=json.dumps(
                {"labels": labels, "task_type": canon_type, "updated": updated}, default=str
            ),
        )
    )
    await session.commit()
    return {"ok": True, "event": "pr_labels_synced", "task_type": canon_type, "updated": updated}
