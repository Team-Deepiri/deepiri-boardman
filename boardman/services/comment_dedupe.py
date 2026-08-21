"""Durable dedupe for comments mirrored onto Plaky.

The poller's seen-sets live in memory, so restarting the process inside the
catch-up window replays recent GitHub events. Status writes are idempotent
(setting "In Progress" twice is invisible), but comments are additive — a
replay posts the same comment to the board a second time, where the whole team
sees it. SyncLog is already written on every mirror, so it doubles as the
durable record of "this exact comment has been posted".
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import or_, select
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


def github_activity_revision_marker(marker: str, body: str) -> str:
    """Identity for one *version* of a comment: the comment, plus what it currently says.

    Plaky's API can create a comment and nothing else -- there is no edit verb and no
    delete verb -- so an edited GitHub comment cannot update the text already on the
    board. Dropping the edit hides a correction; posting it unconditionally duplicates
    the comment on every redelivery. Keying on (comment id, body) gives the only
    behaviour that is both honest and idempotent: each distinct wording of a comment is
    mirrored exactly once, no matter how many times GitHub delivers it, and a redelivered
    `edited` with unchanged text is recognised as already mirrored.
    """
    if not marker:
        return ""
    digest = hashlib.sha256((body or "").encode("utf-8")).hexdigest()[:16]
    return f"{marker}:rev:{digest}"


async def comment_already_synced(session: AsyncSession, action: str, *markers: str) -> bool:
    """True when a SyncLog row for ``action`` already records any of ``markers``.

    Each marker is matched with its surrounding JSON quotes so that a shorter id cannot
    prefix-match a longer one (``issuecomment-123`` vs ``-1234``). Several markers are
    accepted so a caller can check both a comment's plain identity (rows written before
    revisions existed) and the identity of its current wording.
    """
    wanted = [m for m in markers if m]
    if not wanted:
        return False
    row = await session.execute(
        select(SyncLog.id)
        .where(
            SyncLog.action == action,
            or_(*[SyncLog.detail.contains(f'"{m}"') for m in wanted]),
        )
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
    is_revision: bool = False,
    revision_body: str | None = None,
) -> dict:
    """Mirror one GitHub activity item exactly once and record its identity.

    When ``marker`` identifies a comment that can be edited, the dedupe key becomes
    (comment, wording) rather than (comment): each distinct wording reaches the board
    once, and every redelivery of it is recognised. ``is_revision`` says this body is an
    EDIT of a comment already mirrored, which is the one case where the comment's plain
    identity must not block the write -- otherwise the first mirror would silence every
    later correction of that comment.

    ``revision_body`` is the GitHub text the wording key is computed from. It matters
    because `body` is the RENDERED mirror, which carries an "edited" label the original
    did not: hashing that would make an `edited` event with identical text look like new
    wording and post the same sentence to the board twice.
    """
    if not body.strip():
        return {"ok": True, "skipped": True, "message": "empty activity"}
    revision_marker = github_activity_revision_marker(
        marker, body if revision_body is None else revision_body
    )
    # A create also checks the comment's plain identity, so anything mirrored before
    # revision markers existed is not posted a second time. An edit must not: that row
    # is the ORIGINAL wording, and matching it would drop the correction.
    seen = (
        await comment_already_synced(session, action, revision_marker)
        if is_revision
        else await comment_already_synced(session, action, revision_marker, marker)
    )
    if marker and seen:
        return {"ok": True, "skipped": True, "message": "activity already mirrored"}
    result = await plaky.add_comment(task_id, body, board_id=board_id or None)
    session.add(
        SyncLog(
            action=action,
            github_repo=github_repo or None,
            github_ref=github_ref or None,
            plaky_task_id=task_id,
            detail=json.dumps(
                {
                    "marker": revision_marker or marker,
                    "comment_marker": marker,
                    "revision": bool(is_revision),
                    "plaky_ok": result.get("ok"),
                },
                default=str,
            ),
        )
    )
    return {"ok": bool(result.get("ok")), "mirrored": bool(result.get("ok")), "plaky": result}
