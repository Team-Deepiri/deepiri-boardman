import json
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.assignment.qa_picker import build_assignment_field_map
from boardman.database.models import IssueTaskMap, SyncLog
from boardman.github.webhooks import IssueEventPayload
from boardman.plaky.client import PlakyClient
from boardman.plaky.hierarchy import effective_plaky_placement
from boardman.repos_config import get_routing
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
    routing = get_routing(full_name, repo_name, settings.github_org)
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
    assign_fields = await build_assignment_field_map(full_name)
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