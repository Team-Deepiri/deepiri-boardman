"""Stale-PR @mention escalation: nag whoever's turn it is, on a schedule, until they act.

Deliberately event-driven, NOT a polling sweep that re-fetches every open PR's full
timeline from GitHub on every tick -- that doesn't scale (N active PRs x a handful of
API calls each, every sweep, forever). Instead:

- Every comment/review/push webhook Boardman ALREADY receives calls `record_activity()`
  here as a side effect, updating one small DB row per PR. No extra GitHub calls.
- `sweep_due_nudges()` (the periodic loop, boardman/sqlite_worker.py) only ever reads
  that DB table -- zero GitHub calls for anything except the PRs that are actually due,
  where it makes exactly one call each to post the nudge comment.

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
from boardman.github.pr_actions import comment_on_pr

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


async def sweep_due_nudges(session: AsyncSession) -> list[dict]:
    """DB-only scan for rows whose escalation schedule is due, then exactly one
    GitHub API call (the comment post) per PR actually due -- see module docstring
    for why this never re-fetches every tracked PR's state from GitHub."""
    from boardman.assignment.qa_picker import ensure_github_owner_repo

    now = _now()
    rows = (await session.execute(select(PrReviewNudge))).scalars().all()
    results: list[dict] = []
    for row in rows:
        days_waiting = (now - row.last_activity_at).days
        target = _target_stage(days_waiting)
        if target <= row.nudge_stage:
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
        full_name = ensure_github_owner_repo(row.github_repo)
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
