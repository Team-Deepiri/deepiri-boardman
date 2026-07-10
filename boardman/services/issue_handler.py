import json
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.assignment.qa_picker import build_assignment_field_map
from boardman.database.models import IssueTaskMap, SyncLog
from boardman.github.webhooks import IssueEventPayload
from boardman.plaky.board_aware import board_person_field_keys, resolve_group_for_repo
from boardman.plaky.client import PlakyClient
from boardman.plaky.hierarchy import effective_plaky_placement
from boardman.repos_config import get_routing_async
from boardman.settings import settings


ISSUE_LINK_RE = re.compile(r"(?:Closes|Fixes|Resolves)\s+#(\d+)", re.IGNORECASE)


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
            routing_footer += (
                f"**board_id:** `{routing.plaky_board_id}` **group_id:** `{routing.plaky_group_id}`\n"
            )
    description = f"{payload.issue.body or ''}\n\n{payload.issue.html_url}{routing_footer}"

    bid, gid = effective_plaky_placement(routing)
    qa_key_override: str | None = None
    if bid:
        # Category boards: group is named after the repo, person field keys differ
        # per board — resolve both from the live board instead of trusting config.
        gid = await resolve_group_for_repo(bid, repo_name, fallback_group_id=gid, plaky=plaky)
        keys = await board_person_field_keys(bid)
        if keys is not None:
            qa_key_override = keys.get("qa") or ""
    assign_fields = await build_assignment_field_map(full_name, plaky_field_qa_key=qa_key_override)
    result = await plaky.create_task(
        title=title,
        description=description,
        priority="medium",
        board_id=bid,
        group_id=gid,
        field_values=assign_fields if assign_fields else None,
    )

    if not result.get("ok"):
        return result

    task_id = result.get("task", {}).get("id") or result.get("task", {}).get("taskId")
    task_url = result.get("task_url")

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


async def get_linked_issue_numbers(pr_body: Optional[str]) -> list[int]:
    if not pr_body:
        return []
    return [int(m.group(1)) for m in ISSUE_LINK_RE.finditer(pr_body)]


async def find_plaky_task_by_issue(
    repo_name: str, issue_number: int, session: AsyncSession
) -> Optional[IssueTaskMap]:
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