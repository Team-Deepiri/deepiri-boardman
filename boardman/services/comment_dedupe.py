"""Durable dedupe for comments mirrored onto Plaky.

The poller's seen-sets live in memory, so restarting the process inside the
catch-up window replays recent GitHub events. Status writes are idempotent
(setting "In Progress" twice is invisible), but comments are additive — a
replay posts the same comment to the board a second time, where the whole team
sees it. SyncLog is already written on every mirror, so it doubles as the
durable record of "this exact comment has been posted".
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import SyncLog


def github_activity_marker(value: dict | None, *, kind: str, fallback: str = "") -> str:
    """Return a stable identity for a GitHub comment/review across webhook retries."""
    row = value if isinstance(value, dict) else {}
    for key in ("node_id", "id", "database_id", "html_url", "url"):
        raw = str(row.get(key) or "").strip()
        if raw:
            return f"github:{kind}:{raw}"
    return f"github:{kind}:{fallback}" if fallback else ""


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


async def mirror_github_activity(
    session: AsyncSession,
    plaky: Any,
    *,
    task_id: str,
    action: str,
    marker: str,
    body: str,
    board_id: str = "",
    github_repo: str = "",
    github_ref: str = "",
) -> dict:
    """Mirror one GitHub activity item exactly once and record its identity."""
    if not body.strip():
        return {"ok": True, "skipped": True, "message": "empty activity"}
    if marker and await comment_already_synced(session, action, marker):
        return {"ok": True, "skipped": True, "message": "activity already mirrored"}
    result = await plaky.add_comment(task_id, body, board_id=board_id or None)
    session.add(
        SyncLog(
            action=action,
            github_repo=github_repo or None,
            github_ref=github_ref or None,
            plaky_task_id=task_id,
            detail=json.dumps({"marker": marker, "plaky_ok": result.get("ok")}, default=str),
        )
    )
    return {"ok": bool(result.get("ok")), "mirrored": bool(result.get("ok")), "plaky": result}
