import json

from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import SyncLog
from boardman.github.webhooks import PullRequestEventPayload
from boardman.plaky.client import PlakyClient
from boardman.services.issue_handler import get_linked_issue_numbers, find_plaky_task_by_issue
from boardman.settings import settings


async def handle_pr_opened(payload: PullRequestEventPayload, session: AsyncSession) -> dict:
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    pr_url = payload.pull_request.html_url

    linked_issues = await get_linked_issue_numbers(payload.pull_request.body)

    plaky = PlakyClient()
    results = []

    for issue_num in linked_issues:
        mapping = await find_plaky_task_by_issue(repo_name, issue_num, session)
        if mapping:
            comment = f"**PR Opened:** [{pr_number}]({pr_url})"
            await plaky.add_comment(mapping.plaky_task_id, comment)

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
        return {"ok": True, "skipped": True, "message": "No linked issues found"}

    await session.commit()
    return {"ok": True, "linked": results}


async def handle_pr_merged(payload: PullRequestEventPayload, session: AsyncSession) -> dict:
    repo_name = payload.repository.name
    pr_number = payload.pull_request.number
    pr_url = payload.pull_request.html_url

    linked_issues = await get_linked_issue_numbers(payload.pull_request.body)

    plaky = PlakyClient()
    results = []

    for issue_num in linked_issues:
        mapping = await find_plaky_task_by_issue(repo_name, issue_num, session)
        if mapping:
            await plaky.update_task_status(mapping.plaky_task_id, settings.plaky_pr_merge_status)

            log = SyncLog(
                action="pr_merged",
                github_repo=repo_name,
                github_ref=str(pr_number),
                plaky_task_id=mapping.plaky_task_id,
                detail=json.dumps({"issue_number": issue_num, "pr_url": pr_url, "status": settings.plaky_pr_merge_status}),
            )
            session.add(log)
            results.append({"issue": issue_num, "task_id": mapping.plaky_task_id, "status": settings.plaky_pr_merge_status})

    if not linked_issues:
        return {"ok": True, "skipped": True, "message": "No linked issues found"}

    await session.commit()
    return {"ok": True, "updated": results}