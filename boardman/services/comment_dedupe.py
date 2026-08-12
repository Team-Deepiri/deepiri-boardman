"""Durable dedupe for comments mirrored onto Plaky.

The poller's seen-sets live in memory, so restarting the process inside the
catch-up window replays recent GitHub events. Status writes are idempotent
(setting "In Progress" twice is invisible), but comments are additive — a
replay posts the same comment to the board a second time, where the whole team
sees it. SyncLog is already written on every mirror, so it doubles as the
durable record of "this exact comment has been posted".
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import SyncLog


async def comment_already_synced(session: AsyncSession, action: str, marker: str) -> bool:
    """True when a SyncLog row for ``action`` already records ``marker``.

    ``marker`` is matched with its surrounding JSON quotes so that a shorter id
    cannot prefix-match a longer one (``issuecomment-123`` vs ``-1234``).
    """
    if not marker:
        return False
    row = await session.execute(
        select(SyncLog.id)
        .where(SyncLog.action == action, SyncLog.detail.contains(f'"{marker}"'))
        .limit(1)
    )
    return row.first() is not None
