"""Issue-sync helpers shared by the Plaky and ClickUp handlers (no provider-specific code)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import SyncLog


def issue_assignee_login(issue: Any) -> str:
    """First assignee login on the GitHub issue, '' when unassigned."""
    rows = list(getattr(issue, "assignees", None) or [])
    one = getattr(issue, "assignee", None)
    if isinstance(one, dict) and one not in rows:
        rows.insert(0, one)
    for a in rows:
        if isinstance(a, dict) and str(a.get("login") or "").strip():
            return str(a["login"]).strip()
    return ""


async def post_create_patch_failed(session: AsyncSession, task_id: str) -> bool:
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


async def pre_close_status(session: AsyncSession, task_id: str) -> tuple[str | None, str] | None:
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
        # `>=` on the close row that the reopen RESTORED from, not strictly after the
        # reopen. A second delivery of the same reopen (a redelivery, or the poller
        # emitting one the webhook already handled) otherwise excluded the capture row the
        # first delivery had just used, fell through to the assignee ladder, and wrote
        # Assigned over the In QA it had only just restored. The comment mirror dedupes;
        # the status write does not.
        restored_from = (
            await session.execute(
                select(SyncLog.id)
                .where(
                    SyncLog.action == "issue_closed",
                    SyncLog.plaky_task_id == str(task_id),
                    SyncLog.detail.contains('"captured_previous": true'),
                    SyncLog.id < last_reopen,
                )
                .order_by(SyncLog.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        q = q.where(SyncLog.id > (restored_from - 1 if restored_from is not None else last_reopen))
    # Ask for the rows that captured something. The sweep replays `closed` for every
    # closed issue on its page, and by then the task already sits at Completed, so each
    # replay appends a row with a blank capture -- twenty of those (about five hours at
    # the default interval) used to push the only real one out of the window, and the
    # reopen then fell back to the assignee ladder and lost In QA.
    flagged = q.where(SyncLog.detail.contains('"captured_previous": true'))
    rows = list((await session.execute(flagged.order_by(SyncLog.id.desc()).limit(5))).scalars())
    if not rows:
        # Rows written before the flag existed: same scan as before.
        rows = list((await session.execute(q.order_by(SyncLog.id.desc()).limit(20))).scalars())
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
