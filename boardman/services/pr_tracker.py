"""PR tracking for the 'GitHub Repos with a PR' Plaky column."""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import OpenPRTrack
from boardman.github.webhooks import GitHubPullRequest, GitHubRepository
from boardman.plaky.client import PlakyClient
from boardman.settings import settings

_log = logging.getLogger(__name__)


async def upsert_pr_row(
    pr: GitHubPullRequest,
    repo: GitHubRepository,
    session: AsyncSession,
) -> dict:
    """Create or update a row in the PR tracking column when a PR is opened."""
    board_id = settings.plaky_pr_tracking_board_id
    group_id = settings.plaky_pr_tracking_group_id
    if not board_id or not group_id:
        return {"ok": False, "message": "PR tracking board/group not configured"}

    full_name = repo.full_name
    pr_number = pr.number
    pr_url = pr.html_url
    pr_title = pr.title

    stmt = select(OpenPRTrack).where(
        OpenPRTrack.repo_full_name == full_name,
        OpenPRTrack.pr_number == pr_number,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()

    plaky = PlakyClient()
    title = f"{full_name} — PR #{pr_number}: {pr_title}"
    description = f"**Link:** {pr_url}\n**Repo:** {full_name}\n**Number:** #{pr_number}"

    if existing:
        if existing.plaky_item_id:
            item_id = existing.plaky_item_id
        else:
            create_res = await plaky.create_task(
                title=title,
                description=description,
                board_id=board_id,
                group_id=group_id,
            )
            if not create_res.get("ok"):
                return create_res
            item_id = create_res.get("task", {}).get("id") or create_res.get("task", {}).get("itemId")
            if not item_id:
                return {"ok": False, "message": "Created task but no ID returned"}
            existing.plaky_item_id = item_id
            existing.pr_url = pr_url
            existing.pr_title = pr_title
            await session.commit()
            return {"ok": True, "updated": True, "item_id": item_id}
    else:
        create_res = await plaky.create_task(
            title=title,
            description=description,
            board_id=board_id,
            group_id=group_id,
        )
        if not create_res.get("ok"):
            return create_res
        item_id = create_res.get("task", {}).get("id") or create_res.get("task", {}).get("itemId")
        if not item_id:
            return {"ok": False, "message": "Created task but no ID returned"}

        track = OpenPRTrack(
            repo_full_name=full_name,
            pr_number=pr_number,
            plaky_item_id=item_id,
            pr_url=pr_url,
            pr_title=pr_title,
        )
        session.add(track)
        await session.commit()
        return {"ok": True, "created": True, "item_id": item_id}

    return {"ok": True, "skipped": True, "message": "No changes needed"}


async def remove_pr_row(
    pr: GitHubPullRequest,
    repo: GitHubRepository,
    session: AsyncSession,
) -> dict:
    """Remove a row from the PR tracking column when a PR is closed/merged."""
    full_name = repo.full_name
    pr_number = pr.number

    stmt = select(OpenPRTrack).where(
        OpenPRTrack.repo_full_name == full_name,
        OpenPRTrack.pr_number == pr_number,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        if existing.plaky_item_id:
            board_id = settings.plaky_pr_tracking_board_id
            if board_id:
                plaky = PlakyClient()
                del_res = await plaky.delete_board_item(board_id, existing.plaky_item_id)
                if not del_res.get("ok"):
                    _log.warning(
                        "Could not delete Plaky PR tracking item %s: %s",
                        existing.plaky_item_id,
                        del_res.get("message"),
                    )
        await session.delete(existing)
        await session.commit()
        return {"ok": True, "removed": True}

    return {"ok": True, "skipped": True, "message": "No tracking record found"}


async def find_pr_tracker(
    full_name: str,
    pr_number: int,
    session: AsyncSession,
) -> Optional[OpenPRTrack]:
    """Find an existing PR tracking record."""
    stmt = select(OpenPRTrack).where(
        OpenPRTrack.repo_full_name == full_name,
        OpenPRTrack.pr_number == pr_number,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()