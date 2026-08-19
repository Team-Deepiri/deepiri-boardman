import hashlib
import json
import logging
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.assignment.qa_picker import build_repo_field_map
from boardman.database.models import IssueTaskMap, SyncLog
from boardman.github.webhooks import IssueEventPayload
from boardman.plaky.board_aware import resolve_group_for_repo
from boardman.plaky.client import PlakyClient
from boardman.plaky.hierarchy import effective_plaky_placement
from boardman.repos_config import get_routing_async
from boardman.services.comment_dedupe import mirror_github_activity
from boardman.services.sync_state import issue_status_intent, resolve_issue_state
from boardman.settings import settings

_log = logging.getLogger(__name__)

ISSUE_LINK_RE = re.compile(r"(?:Closes|Fixes|Resolves)\s+#(\d+)", re.IGNORECASE)


def _issue_assignee_login(issue: Any) -> str:
    """First assignee login on the GitHub issue, '' when unassigned."""
    rows = list(getattr(issue, "assignees", None) or [])
    one = getattr(issue, "assignee", None)
    if isinstance(one, dict) and one not in rows:
        rows.insert(0, one)
    for a in rows:
        if isinstance(a, dict) and str(a.get("login") or "").strip():
            return str(a["login"]).strip()
    return ""


def native_issue_type_name(issue: Any) -> str:
    """GitHub's native issue Type name ('Feature', 'Bug', ...), '' when unset."""
    t = getattr(issue, "type", None)
    if isinstance(t, dict):
        return str(t.get("name") or "").strip()
    return ""


def _issue_task_text(state: Any, routing: Any | None) -> tuple[str, str]:
    """Build the canonical title/details representation for an issue task."""
    title = f"[{state.repo_name}] {state.title}"
    footer = f"\n\n---\n**GitHub:** {state.repo_full_name}\n" f"**Issue:** #{state.number}\n"
    if routing:
        if getattr(routing, "plaky_table", ""):
            footer += f"**Plaky group (label):** `{routing.plaky_table}`\n"
        if getattr(routing, "category", ""):
            footer += f"**Category:** {routing.category}\n"
        if getattr(routing, "plaky_board_id", "") or getattr(routing, "plaky_group_id", ""):
            footer += (
                f"**board_id:** `{getattr(routing, 'plaky_board_id', '')}` "
                f"**group_id:** `{getattr(routing, 'plaky_group_id', '')}`\n"
            )
    description = f"{state.body}\n\n{state.url}{footer}"
    return title, description


async def _resolve_issue_engineer_id(login: str) -> str:
    if not login:
        return ""
    from boardman.plaky.dynamic_qa_status import (
        github_actor_payload,
        resolve_github_user_to_plaky_user_id,
    )

    return str(
        await resolve_github_user_to_plaky_user_id(github_actor_payload({"login": login})) or ""
    ).strip()


async def _post_create_patch_failed(session: AsyncSession, task_id: str) -> bool:
    """True when the task's post-create field patch is recorded as having failed.

    handle_issue_opened logs `post_create_update_ok`. A replayed `opened` delivery
    exists to repair THAT failure, so it may overwrite board values; when the patch
    succeeded, the board's current values are either GitHub's or a lead's later
    triage, and a replay must not overwrite them.
    """
    q = (
        select(SyncLog)
        .where(SyncLog.action == "issue_created", SyncLog.plaky_task_id == str(task_id))
        .order_by(SyncLog.id.desc())
        .limit(1)
    )
    row = (await session.execute(q)).scalar_one_or_none()
    if not row or not row.detail:
        return False
    try:
        return json.loads(row.detail).get("post_create_update_ok") is False
    except (TypeError, ValueError):
        return False


async def handle_issue_changed(
    payload: IssueEventPayload,
    session: AsyncSession,
    *,
    event_label: str = "issue_changed",
) -> dict[str, Any]:
    """Re-resolve every GitHub-owned issue field after an edit/assignment/label event."""
    state = resolve_issue_state(
        payload.issue,
        repo_full_name=payload.repository.full_name,
        repo_name=payload.repository.name,
    )
    mapping = await find_plaky_task_by_issue(state.repo_name, state.number, session)
    if not mapping or not mapping.plaky_task_id:
        return {"ok": True, "skipped": True, "message": "no Plaky task mapped for this issue"}

    routing = await get_routing_async(state.repo_full_name, state.repo_name, settings.github_org)
    board_id = str(getattr(routing, "plaky_board_id", "") or "").strip()
    sync_text = payload.action == "edited" or event_label == "issue_opened_reconciled"
    title, description = _issue_task_text(state, routing)
    engineer_id = await _resolve_issue_engineer_id(state.assignee_login)

    status_value = ""
    status_field_key: str | None = None
    if board_id:
        from boardman.plaky.dynamic_qa_status import resolve_plaky_status_patch

        resolved = await resolve_plaky_status_patch(board_id, intent=issue_status_intent(state))
        if resolved:
            status_field_key, status_value = resolved[0], resolved[1]
    if not status_value and state.state == "closed":
        status_value = (settings.plaky_status_completed or "").strip()

    from boardman.services.task_mutations import UpdateTaskInput, update_task_internal

    # Priority follows GitHub only when a human SET it there (sidebar field or a
    # priority label). The text-inferred fallback exists for creation; letting it
    # ride every later event would stomp a lead's hand-tuned board value with
    # "Medium" whenever someone touches an unrelated label — and the poller replays
    # `opened` for every issue inside its catch-up window on each restart.
    push_priority = state.priority_explicit
    if not push_priority and event_label == "issue_opened_reconciled":
        push_priority = await _post_create_patch_failed(session, str(mapping.plaky_task_id))
    update = UpdateTaskInput(
        title=title if sync_text else None,
        description=description if sync_text else None,
        status=status_value or None,
        status_plaky_field_key=status_field_key,
        task_type=state.task_type,
        priority=state.priority if push_priority else None,
        engineer_plaky_id=engineer_id or None,
        clear_engineer_assignee=not state.assignee_login,
        plaky_board_id=board_id or None,
        diff_only=True,
    )
    result = await update_task_internal(str(mapping.plaky_task_id), update)

    # Plaky's public API cannot rewrite an existing item's text: OPTIONS on
    # /spaces/{s}/boards/{b}/items/{i} answers "Allow: GET,HEAD,DELETE,OPTIONS", and
    # /fields refuses `title`/`name` ("No field with key or name"). Only creation takes
    # a title, and re-creating the task would destroy its id, comments and PR links.
    # So when the board cannot store the new text, mirror the edit as a comment — the
    # board still shows the change instead of silently keeping a stale title.
    text_ops = (result.get("operations") or {}).get("item_text_fields") or {}
    text_blocked = sync_text and text_ops.get("ok") is False
    text_mirrored = False
    if text_blocked:
        excerpt = (state.body or "").strip()
        if len(excerpt) > 1200:
            excerpt = excerpt[:1200].rstrip() + "…"
        # Marker is content-addressed: a redelivered edit comments once, a genuinely
        # new edit gets its own comment.
        text_digest = hashlib.sha1(
            "\n".join([state.title, state.body]).encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:12]
        mirror = await mirror_github_activity(
            session,
            PlakyClient(),
            task_id=str(mapping.plaky_task_id),
            action="issue_text_changed_comment",
            marker=f"github:issue-text:{state.repo_name}:{state.number}:{text_digest}",
            body=(
                f"**Issue edited on GitHub:** #{state.number}\n\n"
                f"**Title is now:** {state.title}\n\n"
                f"{excerpt}\n\n{state.url}\n\n"
                "_Plaky's API cannot rename an existing item, so this comment carries "
                "the update._"
            ),
            board_id=board_id,
            github_repo=state.repo_name,
            github_ref=str(state.number),
        )
        text_mirrored = bool(mirror.get("ok"))

    # A board that cannot hold item text is a Plaky limitation, not a sync failure:
    # report ok when every writable field landed and the edit was mirrored.
    ok = bool(result.get("ok")) or (text_blocked and text_mirrored)
    detail = {
        "event": payload.action,
        "title": state.title,
        "task_type": state.task_type,
        "priority": state.priority,
        "assignee_login": state.assignee_login,
        "status": status_value,
        "plaky_ok": result.get("ok"),
        "text_mirrored_as_comment": text_mirrored,
    }
    session.add(
        SyncLog(
            action=event_label,
            github_repo=state.repo_name,
            github_ref=str(state.number),
            plaky_task_id=mapping.plaky_task_id,
            detail=json.dumps(detail, default=str),
        )
    )
    await session.commit()
    _log.info(
        "github sync event=%s repo=%s issue=%s task_id=%s type=%s priority=%s assignee=%s status=%s result=%s",
        payload.action,
        state.repo_full_name,
        state.number,
        mapping.plaky_task_id,
        state.task_type,
        state.priority,
        state.assignee_login or "",
        status_value or "unchanged",
        result.get("ok"),
    )
    return {
        "ok": ok,
        "plaky_task_id": mapping.plaky_task_id,
        "event": event_label,
        "task_type": state.task_type,
        "priority": state.priority,
        "status": status_value or None,
        "text_mirrored_as_comment": text_mirrored,
        "mutation": result,
    }


async def handle_issue_opened(payload: IssueEventPayload, session: AsyncSession) -> dict:
    repo_name = payload.repository.name
    issue_number = payload.issue.number

    result = await session.execute(
        select(IssueTaskMap).where(
            IssueTaskMap.github_repo == repo_name,
            IssueTaskMap.github_issue_number == issue_number,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        # A task may have been created successfully while its post-create field patch
        # failed. Replaying the issue-opened delivery must repair current GitHub metadata,
        # not permanently accept the board's default priority/status.
        mapped_id = str(existing.plaky_task_id or "").strip()
        if mapped_id and not mapped_id.startswith("pending:"):
            repaired = await handle_issue_changed(
                payload,
                session,
                event_label="issue_opened_reconciled",
            )
            return {
                "ok": bool(repaired.get("ok")),
                "skipped": True,
                "message": "Issue already mapped; metadata reconciled",
                "reconciled": repaired,
            }
        return {"ok": True, "skipped": True, "message": "Issue creation is already in progress"}

    # Reserve the canonical GitHub identity before the first Plaky POST.  Two
    # different webhook delivery ids for the same issue must not both pass the
    # read-then-create window.  The unique index is the final guard; the savepoint
    # keeps a race from rolling back the surrounding webhook-delivery transaction.
    reservation = IssueTaskMap(
        github_repo=repo_name,
        github_issue_number=issue_number,
        plaky_task_id=f"pending:{uuid.uuid4().hex}",
    )
    try:
        async with session.begin_nested():
            session.add(reservation)
            await session.flush()
    except IntegrityError:
        existing = await find_plaky_task_by_issue(repo_name, issue_number, session)
        if existing:
            return {"ok": True, "skipped": True, "message": "Issue already mapped"}
        raise

    plaky = PlakyClient()
    full_name = payload.repository.full_name
    issue_state = resolve_issue_state(
        payload.issue,
        repo_full_name=full_name,
        repo_name=repo_name,
    )
    title = f"[{repo_name}] {issue_state.title}"
    routing = await get_routing_async(full_name, repo_name, settings.github_org)
    routing_footer = ""
    if routing:
        routing_footer = (
            f"\n\n---\n**Plaky group (label):** `{routing.plaky_table}`\n"
            f"**Category:** {routing.category}\n**GitHub:** {full_name}\n"
        )
        if routing.plaky_board_id or routing.plaky_group_id:
            routing_footer += f"**board_id:** `{routing.plaky_board_id}` **group_id:** `{routing.plaky_group_id}`\n"
    description = f"{issue_state.body}\n\n{issue_state.url}{routing_footer}"

    bid, gid = effective_plaky_placement(routing)
    if bid:
        # Category boards: group is named after the repo — resolve from the live board
        # instead of trusting config.
        gid = await resolve_group_for_repo(bid, repo_name, fallback_group_id=gid, plaky=plaky)

    # Policy (employer review): NO QA at task creation — QA is picked and @mentioned when a
    # PR opens. Priority is inferred from the issue itself; repo tag fields still apply.
    priority = issue_state.priority
    repo_fields = build_repo_field_map(repo_value=full_name)
    result = await plaky.create_task(
        title=title,
        description=description,
        priority=priority.lower(),
        board_id=bid,
        group_id=gid,
        field_values=repo_fields if repo_fields else None,
    )

    if not result.get("ok"):
        await session.delete(reservation)
        return result

    task_id = result.get("task", {}).get("id") or result.get("task", {}).get("taskId")
    task_url = result.get("task_url")
    if not task_id:
        await session.delete(reservation)
        return {"ok": False, "message": "Plaky task create returned no task id"}

    # Explicit defaults on the fresh task: status = NEEDS ASSIGNED (board-resolved) and
    # Type precedence: GitHub's native issue Type (the org uses it; it is the
    # deliberate categorization act) -> labels -> Feature default.
    task_type = issue_state.task_type
    # An issue CREATED with an assignee already has an owner — the board must say
    # Assigned with that person, not NEEDS ASSIGNED with an empty column (verified
    # live on issue #83: GitHub said Blasted-ctrl, the board said nobody).
    engineer_plaky_id = ""
    assignee_login = issue_state.assignee_login
    if assignee_login:
        from boardman.plaky.dynamic_qa_status import (
            github_actor_payload,
            resolve_github_user_to_plaky_user_id,
        )

        engineer_plaky_id = (
            await resolve_github_user_to_plaky_user_id(
                github_actor_payload({"login": assignee_login})
            )
            or ""
        )

    status_key: str | None = None
    status_val = ""
    if bid and task_id:
        from boardman.plaky.dynamic_qa_status import resolve_plaky_status_patch

        intent = "workflow_assigned" if engineer_plaky_id else "workflow_needs_assigned"
        rp = await resolve_plaky_status_patch(bid, intent=intent)
        if rp:
            status_key, status_val = rp[0], rp[1]
    defaults_update: dict[str, Any] = {"ok": True, "skipped": True}
    if task_id:
        from boardman.services.task_mutations import UpdateTaskInput, update_task_internal

        defaults_update = await update_task_internal(
            str(task_id),
            UpdateTaskInput(
                status=status_val or None,
                status_plaky_field_key=status_key,
                task_type=task_type,
                priority=priority,
                engineer_plaky_id=engineer_plaky_id or None,
                plaky_board_id=bid or None,
            ),
        )

    reservation.plaky_task_id = task_id or reservation.plaky_task_id
    reservation.plaky_task_url = task_url

    log = SyncLog(
        action="issue_created",
        github_repo=repo_name,
        github_ref=str(issue_number),
        plaky_task_id=task_id,
        detail=json.dumps(
            {
                "title": title,
                "issue_url": payload.issue.html_url,
                "priority": priority,
                "post_create_update_ok": defaults_update.get("ok"),
            }
        ),
    )
    session.add(log)

    await session.commit()

    return {
        "ok": bool(defaults_update.get("ok")),
        "plaky_task_id": task_id,
        "plaky_task_url": task_url,
        "post_create_update": defaults_update,
    }


async def get_linked_issue_numbers(pr_body: str | None) -> list[int]:
    if not pr_body:
        return []
    return [int(m.group(1)) for m in ISSUE_LINK_RE.finditer(pr_body)]


async def find_plaky_task_by_issue(
    repo_name: str, issue_number: int, session: AsyncSession
) -> IssueTaskMap | None:
    result = await session.execute(
        select(IssueTaskMap).where(
            IssueTaskMap.github_repo == repo_name,
            IssueTaskMap.github_issue_number == issue_number,
        )
    )
    return result.scalar_one_or_none()


async def _pre_close_status(session: AsyncSession, task_id: str) -> tuple[str | None, str] | None:
    """(field_key, value) the task held just before its last close, None if unrecorded.

    Scans back rather than reading one row: a duplicate `closed` delivery (webhook
    redelivery, or webhook + poller both firing) appends a SECOND issue_closed row
    whose capture is blank, because by then the task already sits at Completed. The
    newest row would then hide the only real capture and the reopen would silently
    degrade to the assignee ladder, losing e.g. In QA. Rows older than the last
    reopen belong to a finished cycle and are ignored.
    """
    reopened_q = (
        select(SyncLog.id)
        .where(SyncLog.action == "issue_reopened", SyncLog.plaky_task_id == str(task_id))
        .order_by(SyncLog.id.desc())
        .limit(1)
    )
    last_reopen = (await session.execute(reopened_q)).scalar_one_or_none()

    q = select(SyncLog).where(
        SyncLog.action == "issue_closed", SyncLog.plaky_task_id == str(task_id)
    )
    if last_reopen is not None:
        q = q.where(SyncLog.id > last_reopen)
    rows = (await session.execute(q.order_by(SyncLog.id.desc()).limit(20))).scalars().all()
    for row in rows:
        if not row.detail:
            continue
        try:
            detail = json.loads(row.detail)
        except (TypeError, ValueError):
            continue
        val = str(detail.get("previous_status_value") or "").strip()
        if val:
            return (str(detail.get("previous_status_key") or "").strip() or None, val)
    return None


async def _issue_status_transition(
    payload: IssueEventPayload,
    session: AsyncSession,
    *,
    intents: tuple[str, ...],
    literal_fallback: str,
    action_name: str,
    task_comment: str,
    resolved: tuple[str | None, str] | None = None,
    capture_previous: bool = False,
) -> dict:
    """Shared close/reopen flow: map issue → task, resolve a board status, apply + comment.

    ``resolved`` short-circuits the intent ladder with a known (field_key, value) —
    used by reopen to restore the exact pre-close status. ``capture_previous`` reads
    the status BEFORE writing so a later reopen can resume it.
    """
    repo_name = payload.repository.name
    issue_number = payload.issue.number
    mapping = await find_plaky_task_by_issue(repo_name, issue_number, session)
    if not mapping or not mapping.plaky_task_id:
        return {"ok": True, "skipped": True, "message": "no Plaky task mapped for this issue"}

    routing = await get_routing_async(payload.repository.full_name, repo_name, settings.github_org)
    bid, _gid = effective_plaky_placement(routing)
    bid = (bid or "").strip()

    from boardman.plaky.dynamic_qa_status import resolve_plaky_status_patch

    async def _resolve_by_intent() -> tuple[str | None, str]:
        if bid:
            for intent in intents:
                res = await resolve_plaky_status_patch(bid, intent=intent)
                if res:
                    return res[0], res[1]
        return None, ""

    status_field_key: str | None = None
    target = ""
    if resolved and str(resolved[1] or "").strip():
        status_field_key, target = resolved[0], str(resolved[1]).strip()
    if not target:
        status_field_key, target = await _resolve_by_intent()
    if not target:
        target = (literal_fallback or "").strip()
    if not target:
        return {
            "ok": True,
            "skipped": True,
            "message": f"no status resolvable for {action_name} (board schema or env)",
        }

    previous_status = ""
    if capture_previous and bid and status_field_key:
        from boardman.services.pr_handler import _current_status_value

        previous_status = await _current_status_value(
            PlakyClient(), bid, mapping.plaky_task_id, status_field_key
        )
        if previous_status == str(target):
            previous_status = ""  # already at the target; nothing worth resuming

    from boardman.services.task_mutations import UpdateTaskInput, update_task_internal

    res = await update_task_internal(
        mapping.plaky_task_id,
        UpdateTaskInput(
            status=target,
            plaky_board_id=bid or None,
            status_plaky_field_key=status_field_key,
            diff_only=True,
        ),
    )
    if resolved and not res.get("ok"):
        # The stored pre-close option may no longer exist on the board; fall back
        # to the intent ladder rather than leaving the task stuck on Completed.
        fb_key, fb_target = await _resolve_by_intent()
        if fb_target:
            status_field_key, target = fb_key, fb_target
            res = await update_task_internal(
                mapping.plaky_task_id,
                UpdateTaskInput(
                    status=target,
                    plaky_board_id=bid or None,
                    status_plaky_field_key=status_field_key,
                    diff_only=True,
                ),
            )
    plaky = PlakyClient()
    comment_result = await mirror_github_activity(
        session,
        plaky,
        task_id=mapping.plaky_task_id,
        action=f"{action_name}_comment",
        marker=f"github:issue-state:{repo_name}:{issue_number}:{action_name}",
        body=task_comment,
        board_id=bid,
        github_repo=repo_name,
        github_ref=str(issue_number),
    )
    detail: dict[str, Any] = {
        "issue_url": payload.issue.html_url,
        "plaky_status": target,
        "comment_ok": comment_result.get("ok"),
    }
    if capture_previous:
        detail["previous_status_value"] = previous_status
        detail["previous_status_key"] = status_field_key or ""
    session.add(
        SyncLog(
            action=action_name,
            github_repo=repo_name,
            github_ref=str(issue_number),
            plaky_task_id=mapping.plaky_task_id,
            detail=json.dumps(detail, default=str),
        )
    )
    await session.commit()
    return {"ok": True, "plaky_task_id": mapping.plaky_task_id, "status": target, "plaky": res}


async def handle_issue_closed(payload: IssueEventPayload, session: AsyncSession) -> dict:
    """GitHub issue closed → Completed, remembering the status it held for a reopen."""
    n = payload.issue.number
    return await _issue_status_transition(
        payload,
        session,
        intents=("workflow_completed",),
        literal_fallback=settings.plaky_status_completed,
        action_name="issue_closed",
        task_comment=f"**Issue closed on GitHub:** #{n} — task marked complete by automation.",
        capture_previous=True,
    )


async def handle_issue_reopened(payload: IssueEventPayload, session: AsyncSession) -> dict:
    """GitHub issue reopened → the task RESUMES where it left off.

    Live failure: reopening an unassigned issue wrote In Progress with an empty
    Assignee column — a state the board's own rules forbid. Restore the exact
    pre-close status recorded by handle_issue_closed; when none was recorded
    (legacy closes), derive from the current GitHub assignee: owner → Assigned,
    nobody → NEEDS ASSIGNED. Never a blanket In Progress.
    """
    n = payload.issue.number
    repo_name = payload.repository.name
    has_owner = bool(_issue_assignee_login(payload.issue))
    resolved: tuple[str | None, str] | None = None
    if has_owner:
        # Only an owned issue may resume a working status. If the assignee was removed
        # while the issue sat closed, the board's Assignee column was cleared with it,
        # so restoring "In Progress" would recreate exactly the state Ali reported:
        # a working status with nobody on it. Unowned always means NEEDS ASSIGNED.
        mapping = await find_plaky_task_by_issue(repo_name, n, session)
        if mapping and mapping.plaky_task_id:
            resolved = await _pre_close_status(session, mapping.plaky_task_id)
    intents = ("workflow_assigned",) if has_owner else ("workflow_needs_assigned",)
    return await _issue_status_transition(
        payload,
        session,
        intents=intents,
        literal_fallback="",
        action_name="issue_reopened",
        task_comment=f"**Issue reopened on GitHub:** #{n} — task resumed by automation.",
        resolved=resolved,
    )


async def handle_issue_labels_changed(payload: IssueEventPayload, session: AsyncSession) -> dict:
    """GitHub `labeled`/`unlabeled` → re-mirror the linked task's Type.

    People label AFTER creating: issue #80 was filed bare and got its `bug` label 75
    seconds later, so the poller raced it and the task said Story indefinitely. Labels on
    GitHub are the team's explicit categorization act — the source of truth for Type — so
    a label change must reach the board instead of freezing whatever the creation-time
    race produced.

    Priority follows the same rule: it re-syncs ONLY when GitHub carries an explicit
    signal (the sidebar Priority field or a priority label). Text-inferred priority
    never rides these events, so a lead's hand-tuned board value survives.
    """
    return await handle_issue_changed(
        payload,
        session,
        event_label="issue_labels_synced",
    )


async def handle_issue_edited(payload: IssueEventPayload, session: AsyncSession) -> dict:
    return await handle_issue_changed(payload, session, event_label="issue_edited_synced")
