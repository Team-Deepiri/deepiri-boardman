"""Stale-PR @mention escalation: nag whoever's turn it is, on a schedule, until they act.

Deliberately event-driven, NOT a polling sweep that re-fetches every open PR's full
timeline from GitHub on every tick -- that doesn't scale (N active PRs x a handful of
API calls each, every sweep, forever). Instead:

- Every comment/review/push webhook Boardman ALREADY receives calls `record_activity()`
  here as a side effect, updating one small DB row per PR. No extra GitHub calls.
- `sweep_due_nudges()` (the periodic loop, boardman/sqlite_worker.py) only ever reads
  that DB table -- zero GitHub calls for anything except the PRs that are actually due,
  where it makes one call to check the PR is still open and one to post the comment.

That last part is the point. A row outlives the PR it describes: nothing in the event
stream says "this is over", and a merge doesn't stop the clock. So a PR merged on day 2
kept its escalation state, and the sweep went on @mentioning a QA engineer on it at days
3, 6, 9, 12, 15 and then every day after that -- for a PR that had already shipped
(deepiri-mudspeed#50 merged 2026-09-14, nudged 09-17, 09-20, 09-23, 09-26). The DB says
"waiting on QA"; only GitHub says whether there is still anything to wait for. So every
row the sweep is about to act on gets exactly one live state check, and a PR that is
merged or closed has its row retired instead of mentioned.

Also deliberately does NOT resolve who's who from team_assignments.yml/Plaky ids: the
developer is the PR's own author (GitHub's own `pull_request.user.login`), and the
primary QA is whoever Boardman actually requested as reviewer on GitHub (the same
`qa_login` `_assign_qa_for_pr` already computed) -- both facts GitHub already knows,
with no roster/config lookup needed to reconstruct them later.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import PrReviewNudge
from boardman.github.pr_actions import (
    PR_STATE_UNKNOWN,
    PR_TERMINAL,
    comment_on_pr,
    pr_lifecycle_state,
)

_log = logging.getLogger(__name__)

# A commenter who is neither the developer nor the primary QA is a drive-by until they
# show up this many times -- then they're treated as chipping in on the QA side too.
_CHIP_IN_THRESHOLD = 2

# Escalation cadence (days since it became the other side's turn): pings at 3, 6, 9,
# 12, 15, then daily. See _target_stage.
_SCHEDULED_DAYS = (3, 6, 9, 12, 15)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def parse_github_timestamp(value: str) -> datetime:
    """GitHub's ISO8601 ("...Z") timestamp -> naive UTC datetime, matching every other
    DateTime column in this codebase (all naive-UTC, e.g. datetime.utcnow() defaults).
    Falls back to now() on anything unparseable rather than raising -- a webhook
    delivery must not be lost over a timestamp format quirk."""
    s = (value or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return _now()
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _target_stage(days_waiting: int) -> int:
    """How many nudges SHOULD have gone out by now, given `days_waiting` since the
    turn flipped. 0 before day 3; 1-5 at the day-3/6/9/12/15 marks; daily after that."""
    if days_waiting < _SCHEDULED_DAYS[0]:
        return 0
    if days_waiting <= _SCHEDULED_DAYS[-1]:
        return days_waiting // _SCHEDULED_DAYS[0]
    return len(_SCHEDULED_DAYS) + (days_waiting - _SCHEDULED_DAYS[-1])


async def _get_row(
    session: AsyncSession, github_repo: str, github_pr_number: int
) -> PrReviewNudge | None:
    q = select(PrReviewNudge).where(
        PrReviewNudge.github_repo == github_repo,
        PrReviewNudge.github_pr_number == github_pr_number,
    )
    return (await session.execute(q)).scalar_one_or_none()


async def ensure_tracked(
    session: AsyncSession,
    *,
    github_repo: str,
    github_pr_number: int,
    developer_login: str,
    primary_qa_login: str,
) -> None:
    """Create the baseline row the first time a PR gets a QA assigned (there's nothing
    to nudge about before that -- no review relationship exists yet), or refresh the
    logins on an existing row (a QA reassignment) without disturbing the escalation
    clock that's already running.
    """
    dev = (developer_login or "").strip()
    qa = (primary_qa_login or "").strip()
    if not dev:
        return
    row = await _get_row(session, github_repo, github_pr_number)
    if row is None:
        session.add(
            PrReviewNudge(
                github_repo=github_repo,
                github_pr_number=github_pr_number,
                developer_login=dev,
                primary_qa_login=qa or None,
                waiting_on="qa",
                last_activity_at=_now(),
                nudge_stage=0,
            )
        )
        return
    row.developer_login = dev
    if qa:
        row.primary_qa_login = qa


async def record_activity(
    session: AsyncSession,
    *,
    github_repo: str,
    github_pr_number: int,
    actor_login: str,
    at: datetime,
) -> None:
    """Called from the comment/review/push webhook handlers as a side effect. A no-op
    if this PR isn't tracked yet (no QA assigned) -- there's no "other side" to flip
    the turn to."""
    login = (actor_login or "").strip().casefold()
    if not login:
        return
    row = await _get_row(session, github_repo, github_pr_number)
    if row is None:
        return

    if login == row.developer_login.strip().casefold():
        role = "developer"
    elif row.primary_qa_login and login == row.primary_qa_login.strip().casefold():
        role = "qa"
    else:
        extra: dict[str, int] = {}
        if row.extra_qa_json:
            try:
                extra = json.loads(row.extra_qa_json)
            except (ValueError, TypeError):
                extra = {}
        count = int(extra.get(login, 0)) + 1
        extra[login] = count
        row.extra_qa_json = json.dumps(extra)
        role = "qa" if count >= _CHIP_IN_THRESHOLD else "other"

    if role == "other":
        return

    new_waiting_on = "developer" if role == "qa" else "qa"
    if new_waiting_on != row.waiting_on or at > row.last_activity_at:
        row.waiting_on = new_waiting_on
        row.last_activity_at = at
        row.nudge_stage = 0
        row.last_nudge_at = None


def _recipients(row: PrReviewNudge) -> list[str]:
    if row.waiting_on == "developer":
        return [row.developer_login] if row.developer_login else []
    recipients: list[str] = []
    if row.primary_qa_login:
        recipients.append(row.primary_qa_login)
    if row.extra_qa_json:
        try:
            extra = json.loads(row.extra_qa_json)
        except (ValueError, TypeError):
            extra = {}
        for login, count in (extra.items() if isinstance(extra, dict) else []):
            if isinstance(count, int) and count >= _CHIP_IN_THRESHOLD and login not in recipients:
                recipients.append(login)
    return recipients


def _compose_message(waiting_on: str, days: int, recipients: list[str]) -> str:
    mentions = " ".join(f"@{login}" for login in recipients)
    side = "QA review" if waiting_on == "qa" else "the developer to follow up"
    return (
        f"⏰ This PR has been waiting on {side} for {days} day(s) with no response. "
        f"{mentions} — friendly nudge to take a look."
    )


async def retire(session: AsyncSession, *, github_repo: str, github_pr_number: int) -> bool:
    """Stop tracking a PR that has reached a terminal state (merged, or closed unmerged).

    Escalation state is only meaningful while there is something left to do, and a nudge
    aimed at a PR nobody can act on any more is a mention with no recipient. Called from
    the merge/close handlers so the row normally goes away at the moment the PR does,
    and from the sweep for anything those events missed (a dropped delivery, a PR that
    merged while the worker was down).

    Safe to call for a PR that was never tracked, and safe to call twice. Deleting rather
    than flagging the row keeps the table's meaning single ("PRs someone is waiting on"),
    and a reopened PR is re-tracked from scratch by the reopen path's QA assignment --
    which is correct anyway: a PR that was reopened is a fresh review conversation, and
    the old escalation clock described a PR that no longer exists.
    """
    row = await _get_row(session, github_repo, github_pr_number)
    if row is None:
        return False
    await session.delete(row)
    _log.info(
        "pr_review_nudges: retired escalation state for closed PR %s#%s",
        github_repo,
        github_pr_number,
    )
    return True


async def sweep_due_nudges(session: AsyncSession) -> list[dict]:
    """DB-only scan for rows whose escalation schedule is due, then for each of those
    exactly one GitHub state check and, only if the PR is still active, one comment.

    The state check is what stops this from @mentioning people on a merged PR: a row
    says who owes a review, never whether there is still a review to owe. Reading rows
    that are not due still costs zero GitHub calls -- the point of the table is that the
    common case is a local comparison.
    """
    from boardman.assignment.qa_picker import ensure_github_owner_repo

    now = _now()
    rows = (await session.execute(select(PrReviewNudge))).scalars().all()
    results: list[dict] = []
    for row in rows:
        days_waiting = (now - row.last_activity_at).days
        target = _target_stage(days_waiting)
        if target <= row.nudge_stage:
            continue
        full_name = ensure_github_owner_repo(row.github_repo)

        # Authoritative, not the stored clock. Deliberately asked per DUE row only.
        lifecycle = await pr_lifecycle_state(full_name, row.github_pr_number)
        if lifecycle == PR_TERMINAL:
            retired = await retire(
                session,
                github_repo=row.github_repo,
                github_pr_number=row.github_pr_number,
            )
            results.append(
                {
                    "github_repo": row.github_repo,
                    "github_pr_number": row.github_pr_number,
                    "days_waiting": days_waiting,
                    "stage": target,
                    "ok": True,
                    "skipped": "pr_not_active",
                    "retired": retired,
                }
            )
            continue
        if lifecycle == PR_STATE_UNKNOWN:
            # No comment, and no stage advance either: the next sweep re-reads this row
            # and tries again. Retiring it here would silently drop escalation for a PR
            # that is still open because GitHub was briefly unreachable.
            _log.warning(
                "pr_review_nudges: %s#%s is due (stage %d) but its PR state could not be "
                "read; skipping this sweep without advancing the stage",
                row.github_repo,
                row.github_pr_number,
                target,
            )
            continue

        recipients = _recipients(row)
        if not recipients:
            _log.info(
                "pr_review_nudges: PR %s#%s is due (stage %d) but has no resolvable "
                "recipient (waiting_on=%s) -- skipping without advancing the stage",
                row.github_repo,
                row.github_pr_number,
                target,
                row.waiting_on,
            )
            continue
        message = _compose_message(row.waiting_on, days_waiting, recipients)
        res = await comment_on_pr(full_name, row.github_pr_number, message)
        outcome = {
            "github_repo": row.github_repo,
            "github_pr_number": row.github_pr_number,
            "waiting_on": row.waiting_on,
            "days_waiting": days_waiting,
            "stage": target,
            "recipients": recipients,
            "ok": bool(res.get("ok")),
        }
        if res.get("ok"):
            row.nudge_stage = target
            row.last_nudge_at = now
        else:
            _log.warning(
                "pr_review_nudges: nudge comment failed for %s#%s: %s",
                row.github_repo,
                row.github_pr_number,
                res.get("message"),
            )
        results.append(outcome)
    await session.commit()
    return results
