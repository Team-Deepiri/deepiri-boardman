"""Cleanup/archival sweep for Plaky tasks the PR pipeline created or linked.

Two independent passes, run periodically by boardman-worker (see sqlite_worker.py):

  1. ``cleanup_orphaned_pr_tasks`` — a task CREATED for a PR that matched nothing
     existing (``PrTaskLifecycle.origin == "created"``) is provisional. If it is still
     sitting there past its TTL, it is deleted from Plaky and the tracking row is
     stamped ``deleted_at`` (kept, not removed, as an audit trail of what was cleaned up
     and when).
  2. ``archive_completed_matched_tasks`` — a task the PR pipeline LINKED to an already
     existing task (``pr_task_links.link_source`` not one of the created/pending/
     superseded markers) is real work and is never deleted. Once its PR is done (merged
     or closed) and the task itself reached a finished-looking status, it is moved off
     its working board into the configured archive board/group and removed from the
     original — "moved" because Plaky's public API has no board-to-board move endpoint,
     so this recreates the item on the archive board (same title/description) then
     deletes the original.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import PrTaskLifecycle, PullRequestTaskLink
from boardman.plaky.client import PlakyClient
from boardman.services.plaky_group_reorder import _item_looks_done
from boardman.settings import settings

_log = logging.getLogger(__name__)

# link_source values that do NOT represent a real, pre-existing match.
_NON_MATCHED_LINK_SOURCES = frozenset(
    {"pr_task_pending", "pr_task_created", "superseded_by_issue_link"}
)


def _done_markers() -> tuple[str, ...]:
    markers = tuple(
        x.strip().casefold()
        for x in (settings.plaky_reorder_done_status_markers or "").split(",")
        if x.strip()
    )
    return markers or ("done", "complete", "closed", "resolved")


async def cleanup_orphaned_pr_tasks(
    session: AsyncSession, plaky: PlakyClient | None = None
) -> dict:
    if not settings.pr_task_cleanup_enabled:
        return {"ok": True, "skipped": True, "message": "pr_task_cleanup disabled"}

    plaky = plaky or PlakyClient()
    now = datetime.utcnow()
    rows = (
        (
            await session.execute(
                select(PrTaskLifecycle).where(
                    PrTaskLifecycle.origin == "created",
                    PrTaskLifecycle.deleted_at.is_(None),
                    PrTaskLifecycle.cleanup_due_at.is_not(None),
                    PrTaskLifecycle.cleanup_due_at <= now,
                )
            )
        )
        .scalars()
        .all()
    )

    deleted = 0
    failed = 0
    for row in rows:
        board_id = (row.plaky_board_id or "").strip()
        if not board_id:
            row.deleted_at = now
            continue
        res = await plaky.delete_board_item(board_id, row.plaky_task_id)
        if res.get("ok"):
            row.deleted_at = now
            deleted += 1
        else:
            failed += 1
            _log.warning(
                "pr_task cleanup: failed to delete plaky task %s (repo=%s pr=%s): %s",
                row.plaky_task_id,
                row.github_repo,
                row.github_pr_number,
                res.get("message"),
            )
    await session.commit()
    return {"ok": True, "checked": len(rows), "deleted": deleted, "failed": failed}


async def archive_completed_matched_tasks(
    session: AsyncSession, plaky: PlakyClient | None = None
) -> dict:
    archive_board = (settings.pr_task_archive_board_id or "").strip()
    if not archive_board:
        return {"ok": True, "skipped": True, "message": "pr_task_archive_board_id not configured"}

    plaky = plaky or PlakyClient()
    archive_group = (settings.pr_task_archive_group_id or "").strip() or None
    markers = _done_markers()

    rows = (
        (
            await session.execute(
                select(PullRequestTaskLink).where(
                    PullRequestTaskLink.merged_at.is_not(None),
                    PullRequestTaskLink.link_source.notin_(_NON_MATCHED_LINK_SOURCES),
                )
            )
        )
        .scalars()
        .all()
    )

    from boardman.repos_config import get_routing_async

    archived = 0
    checked = 0
    owner = (settings.github_bare_repo_owner or settings.github_org or "").strip()
    board_id_cache: dict[str, str] = {}
    for row in rows:
        task_id = (row.plaky_task_id or "").strip()
        if not task_id or task_id == archive_board:
            continue
        repo_name = row.github_repo
        full_name = f"{owner}/{repo_name}" if owner else repo_name
        home_board = board_id_cache.get(repo_name)
        if home_board is None:
            routing = await get_routing_async(full_name, repo_name, settings.github_org)
            home_board = (
                (routing.plaky_board_id if routing and routing.plaky_board_id else "") or ""
            ).strip()
            board_id_cache[repo_name] = home_board
        if not home_board:
            continue

        checked += 1
        got = await plaky.get_task(task_id)
        if not got.get("ok"):
            continue
        task = got.get("task") if isinstance(got.get("task"), dict) else {}
        if not _item_looks_done(task, markers):
            continue

        title = str(task.get("title") or task.get("name") or f"PR #{row.github_pr_number}")
        description = str(task.get("description") or "")
        note = (
            f"\n\nArchived by boardman: PR #{row.github_pr_number} in {row.github_repo} "
            f"is done and this task reached a completed status."
        )
        created = await plaky.create_task(
            title,
            description + note,
            board_id=archive_board,
            group_id=archive_group,
        )
        if not created.get("ok"):
            _log.warning(
                "pr_task archive: failed to recreate task %s on archive board: %s",
                task_id,
                created.get("message"),
            )
            continue

        deleted = await plaky.delete_board_item(home_board, task_id)
        if deleted.get("ok"):
            archived += 1
    await session.commit()
    return {"ok": True, "checked": checked, "archived": archived}
