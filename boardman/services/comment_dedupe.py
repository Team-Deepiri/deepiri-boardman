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


# Separates the timestamp from the body in the revision digest, so a body that starts
# with a date cannot collide with a different timestamp and an empty-ish body.
_REVISION_SEPARATOR = "\n--boardman-rev--\n"


def github_activity_revision_marker(marker: str, body: str, edited_at: str = "") -> str:
    """Identity for one *version* of a comment: the comment, plus what it currently says.

    Plaky's API can create a comment and nothing else -- there is no edit verb and no
    delete verb -- so an edited GitHub comment cannot update the text already on the
    board. Dropping the edit hides a correction; posting it unconditionally duplicates
    the comment on every redelivery. Keying on (comment id, when it was edited, what it
    says) gives the only behaviour that is both honest and idempotent: each distinct
    version reaches the board once however many times GitHub delivers it, and a
    redelivered `edited` carrying the same updated_at is recognised as already mirrored.
    """
    if not marker:
        return ""
    # `edited_at` (GitHub's comment.updated_at) is what separates a REDELIVERY from a
    # REVERT. Hashing the body alone made them identical, so editing a comment back to an
    # earlier wording matched the earlier row and was skipped -- leaving the board's
    # newest entry showing text the author had taken back. GitHub stamps a new
    # updated_at for the revert, so it mirrors; a redelivery carries the same one and
    # still dedupes. Without it, body-only is the old behaviour.
    payload = (edited_at or "") + _REVISION_SEPARATOR + (body or "")
    # 32 hex chars (128 bits) rather than 16 (64 bits) -- a dedupe key needs enough
    # collision resistance that two distinct edits never fold into the same row.
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{marker}:rev:{digest}"


def _github_activity_revision_marker_legacy(marker: str, body: str, edited_at: str = "") -> str:
    """The pre-widening 16-hex-char digest, for the seen-check only.

    `SyncLog.detail` is matched by exact substring (`comment_already_synced`), so a row
    written before the digest widened above is keyed on the OLD 16-char value and would
    never match the new 32-char one -- every already-mirrored edited comment would look
    unseen and get reposted once, right after this deploys. Checked alongside the new
    marker so history keeps matching; never used to WRITE a new row.
    """
    if not marker:
        return ""
    payload = (edited_at or "") + _REVISION_SEPARATOR + (body or "")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{marker}:rev:{digest}"


def edit_changed_the_text(payload: Any) -> bool:
    """Did this `edited` delivery change the comment's TEXT?

    GitHub puts `changes.body.from` on the event only when the body changed, so an edit
    that touched nothing else -- saving without a change, an edit to some other field --
    arrives with `changes` present and no `body` in it. Plaky cannot edit a posted
    comment, so a mirror is an additional entry on the card: treating those as new wording
    posts the same sentence twice, because `updated_at` moved even though nothing did.

    A payload carrying no `changes` at all (the poller synthesises those) is taken at its
    word and mirrored. The revision key still stops a redelivery of it landing twice.
    """
    changes = getattr(payload, "changes", None)
    if not isinstance(changes, dict):
        return True
    return "body" in changes


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
    # `escape` matters: a marker falls back to the comment BODY when GitHub sends no id,
    # and a body containing % or _ is a LIKE wildcard. Unescaped, "50%_done" matches rows
    # it has nothing to do with, and the mirror is silently dropped as already synced.
    row = await session.execute(
        select(SyncLog.id)
        .where(
            SyncLog.action == action,
            or_(*[SyncLog.detail.contains(f'"{m}"', autoescape=True) for m in wanted]),
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
    edited_at: str = "",
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
    wording = body if revision_body is None else revision_body
    revision_marker = github_activity_revision_marker(marker, wording, edited_at)
    legacy_revision_marker = _github_activity_revision_marker_legacy(marker, wording, edited_at)
    # A create also checks the comment's plain identity, so anything mirrored before
    # revision markers existed is not posted a second time. An edit must not: that row
    # is the ORIGINAL wording, and matching it would drop the correction. The legacy
    # marker is checked the same way, for the same reason -- rows written under the old
    # 16-char digest.
    seen = (
        await comment_already_synced(session, action, revision_marker, legacy_revision_marker)
        if is_revision
        else await comment_already_synced(
            session, action, revision_marker, legacy_revision_marker, marker
        )
    )
    if marker and seen:
        return {"ok": True, "skipped": True, "message": "activity already mirrored"}
    result = await plaky.add_comment(task_id, body, board_id=board_id or None)
    # A refused post left nothing on the card, so the row must not claim the identity that
    # dedupes it. Written under `action`, one transient Plaky failure suppressed that
    # comment permanently: every redelivery matched the row and skipped. The attempt is
    # still recorded, under an action nothing dedupes against, so it stays auditable.
    posted = bool(result.get("ok"))
    session.add(
        SyncLog(
            action=action if posted else f"{action}_failed",
            github_repo=github_repo or None,
            github_ref=github_ref or None,
            plaky_task_id=task_id,
            detail=json.dumps(
                {
                    "marker": revision_marker or marker,
                    "comment_marker": marker,
                    "revision": bool(is_revision),
                    "plaky_ok": posted,
                },
                default=str,
            ),
        )
    )
    return {"ok": posted, "mirrored": posted, "plaky": result}
