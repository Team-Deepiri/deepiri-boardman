import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.assignment.qa_picker import build_repo_field_map
from boardman.database.models import IssueTaskMap, SyncLog
from boardman.github.pr_signals import infer_task_type_from_pr
from boardman.github.pr_signals import pr_label_names as issue_label_names
from boardman.github.webhooks import IssueEventPayload
from boardman.plaky.board_aware import resolve_group_for_repo
from boardman.plaky.client import PlakyClient
from boardman.plaky.hierarchy import effective_plaky_placement
from boardman.repos_config import get_routing_async
from boardman.services.priority_rules import infer_priority_from_text
from boardman.settings import settings

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
        return {"ok": True, "skipped": True, "message": "Issue already mapped"}

    plaky = PlakyClient()
    full_name = payload.repository.full_name
    title = f"[{repo_name}] {payload.issue.title}"
    routing = await get_routing_async(full_name, repo_name, settings.github_org)
    routing_footer = ""
    if routing:
        routing_footer = (
            f"\n\n---\n**Plaky group (label):** `{routing.plaky_table}`\n"
            f"**Category:** {routing.category}\n**GitHub:** {full_name}\n"
        )
        if routing.plaky_board_id or routing.plaky_group_id:
            routing_footer += f"**board_id:** `{routing.plaky_board_id}` **group_id:** `{routing.plaky_group_id}`\n"
    description = f"{payload.issue.body or ''}\n\n{payload.issue.html_url}{routing_footer}"

    bid, gid = effective_plaky_placement(routing)
    if bid:
        # Category boards: group is named after the repo — resolve from the live board
        # instead of trusting config.
        gid = await resolve_group_for_repo(bid, repo_name, fallback_group_id=gid, plaky=plaky)

    # Policy (employer review): NO QA at task creation — QA is picked and @mentioned when a
    # PR opens. Priority is inferred from the issue itself; repo tag fields still apply.
    labels = issue_label_names(payload.issue.labels)
    priority = infer_priority_from_text(payload.issue.title, payload.issue.body, labels)
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
        return result

    task_id = result.get("task", {}).get("id") or result.get("task", {}).get("taskId")
    task_url = result.get("task_url")

    # Explicit defaults on the fresh task: status = NEEDS ASSIGNED (board-resolved) and
    # Type precedence: GitHub's native issue Type (the org uses it; it is the
    # deliberate categorization act) -> labels -> Feature default.
    task_type = (
        native_issue_type_name(payload.issue) or infer_task_type_from_pr(None, labels) or "Feature"
    )
    # An issue CREATED with an assignee already has an owner — the board must say
    # Assigned with that person, not NEEDS ASSIGNED with an empty column (verified
    # live on issue #83: GitHub said Blasted-ctrl, the board said nobody).
    engineer_plaky_id = ""
    assignee_login = _issue_assignee_login(payload.issue)
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
    if task_id:
        from boardman.services.task_mutations import UpdateTaskInput, update_task_internal

        await update_task_internal(
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

    mapping = IssueTaskMap(
        github_repo=repo_name,
        github_issue_number=issue_number,
        plaky_task_id=task_id or "",
        plaky_task_url=task_url,
    )
    session.add(mapping)

    log = SyncLog(
        action="issue_created",
        github_repo=repo_name,
        github_ref=str(issue_number),
        plaky_task_id=task_id,
        detail=json.dumps({"title": title, "issue_url": payload.issue.html_url}),
    )
    session.add(log)

    await session.commit()

    return {"ok": True, "plaky_task_id": task_id, "plaky_task_url": task_url}


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


async def _issue_status_transition(
    payload: IssueEventPayload,
    session: AsyncSession,
    *,
    intents: tuple[str, ...],
    literal_fallback: str,
    action_name: str,
    task_comment: str,
) -> dict:
    """Shared close/reopen flow: map issue → task, resolve a board status, apply + comment."""
    repo_name = payload.repository.name
    issue_number = payload.issue.number
    mapping = await find_plaky_task_by_issue(repo_name, issue_number, session)
    if not mapping or not mapping.plaky_task_id:
        return {"ok": True, "skipped": True, "message": "no Plaky task mapped for this issue"}

    routing = await get_routing_async(payload.repository.full_name, repo_name, settings.github_org)
    bid, _gid = effective_plaky_placement(routing)
    bid = (bid or "").strip()

    status_field_key: str | None = None
    target = ""
    if bid:
        from boardman.plaky.dynamic_qa_status import resolve_plaky_status_patch

        for intent in intents:
            res = await resolve_plaky_status_patch(bid, intent=intent)
            if res:
                status_field_key, target = res[0], res[1]
                break
    if not target:
        target = (literal_fallback or "").strip()
    if not target:
        return {
            "ok": True,
            "skipped": True,
            "message": f"no status resolvable for {action_name} (board schema or env)",
        }

    from boardman.services.task_mutations import UpdateTaskInput, update_task_internal

    res = await update_task_internal(
        mapping.plaky_task_id,
        UpdateTaskInput(
            status=target,
            plaky_board_id=bid or None,
            status_plaky_field_key=status_field_key,
        ),
    )
    plaky = PlakyClient()
    await plaky.add_comment(mapping.plaky_task_id, task_comment, board_id=bid or None)
    session.add(
        SyncLog(
            action=action_name,
            github_repo=repo_name,
            github_ref=str(issue_number),
            plaky_task_id=mapping.plaky_task_id,
            detail=json.dumps(
                {"issue_url": payload.issue.html_url, "plaky_status": target}, default=str
            ),
        )
    )
    await session.commit()
    return {"ok": True, "plaky_task_id": mapping.plaky_task_id, "status": target, "plaky": res}


async def handle_issue_closed(payload: IssueEventPayload, session: AsyncSession) -> dict:
    """GitHub issue closed → linked Plaky task moves to Completed."""
    n = payload.issue.number
    return await _issue_status_transition(
        payload,
        session,
        intents=("workflow_completed",),
        literal_fallback=settings.plaky_status_completed,
        action_name="issue_closed",
        task_comment=f"**Issue closed on GitHub:** #{n} — task marked complete by automation.",
    )


async def handle_issue_reopened(payload: IssueEventPayload, session: AsyncSession) -> dict:
    """GitHub issue reopened → linked Plaky task moves back to In Progress (or Assigned)."""
    n = payload.issue.number
    return await _issue_status_transition(
        payload,
        session,
        intents=("workflow_in_progress", "workflow_assigned"),
        literal_fallback="",
        action_name="issue_reopened",
        task_comment=f"**Issue reopened on GitHub:** #{n} — task revived by automation.",
    )


async def handle_issue_labels_changed(payload: IssueEventPayload, session: AsyncSession) -> dict:
    """GitHub `labeled`/`unlabeled` → re-mirror the linked task's Type.

    People label AFTER creating: issue #80 was filed bare and got its `bug` label 75
    seconds later, so the poller raced it and the task said Story indefinitely. Labels on
    GitHub are the team's explicit categorization act — the source of truth for Type — so
    a label change must reach the board instead of freezing whatever the creation-time
    race produced.

    Only Type is touched. Priority may have been hand-tuned by a lead after triage, and
    label changes carry no signal about it.
    """
    repo_name = payload.repository.name
    issue_number = payload.issue.number
    mapping = await find_plaky_task_by_issue(repo_name, issue_number, session)
    if not mapping or not mapping.plaky_task_id:
        return {"ok": True, "skipped": True, "message": "no Plaky task mapped for this issue"}

    labels = issue_label_names(payload.issue.labels)
    task_type = (
        native_issue_type_name(payload.issue) or infer_task_type_from_pr(None, labels) or "Feature"
    )

    routing = await get_routing_async(payload.repository.full_name, repo_name, settings.github_org)
    bid = ((routing.plaky_board_id if routing and routing.plaky_board_id else "") or "").strip()

    from boardman.services.task_mutations import UpdateTaskInput, update_task_internal

    # An assignee added AFTER creation flows too (same race as labels: people assign
    # from the GitHub UI seconds later). Fill-only: an existing Plaky assignee is
    # curated state and is never overwritten or cleared from here.
    engineer_plaky_id = ""
    assignee_login = _issue_assignee_login(payload.issue)
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

    res = await update_task_internal(
        str(mapping.plaky_task_id),
        UpdateTaskInput(
            task_type=task_type,
            engineer_plaky_id=engineer_plaky_id or None,
            plaky_board_id=bid or None,
        ),
    )
    session.add(
        SyncLog(
            action="issue_labels_synced",
            github_repo=repo_name,
            github_ref=str(issue_number),
            plaky_task_id=mapping.plaky_task_id,
            detail=json.dumps(
                {"labels": labels, "task_type": task_type, "plaky_ok": res.get("ok")},
                default=str,
            ),
        )
    )
    await session.commit()
    return {
        "ok": True,
        "plaky_task_id": mapping.plaky_task_id,
        "event": "issue_labels_synced",
        "task_type": task_type,
    }
