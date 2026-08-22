"""Track GitHub PR ↔ Plaky task links for multi-PR tasks and merge-gated completion."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import PullRequestTaskLink

# Stamped on a link retired because the PR gained an explicit issue relationship. It
# distinguishes "this PR is closed for now" from "this card is not this PR's card any
# more", which reopening must not undo.
_SUPERSEDED_LINK_SOURCE = "superseded_by_issue_link"
# Stamped on a link inferred from the BRANCH NAME rather than a written closing keyword.
# The link itself is wanted -- `issue-94-add-retries` is a convention this team uses, and
# the PR's comments and reviews belong on that task. What it is not is a statement that
# merging the PR finishes the issue: GitHub closes an issue for `Fixes #94` and never for
# a branch name, and Boardman should not claim more than the author wrote.
_BRANCH_REF_LINK_SOURCE = "branch_ref"
# Stamped on a link from a closing keyword in the PR TITLE. A person wrote it, so it is a
# real link and everything the PR does belongs on that task. GitHub still does not act on
# it -- only the description and commit messages close an issue -- so the issue stays open
# after the merge, and completing the task from the title alone would put the board in a
# state the issue's own events then contradict.
_TITLE_REF_LINK_SOURCE = "issue_keyword_title"
# Links a merge must not treat as "the author said this PR finishes that issue".
_WEAK_COMPLETION_LINK_SOURCES = (_BRANCH_REF_LINK_SOURCE, _TITLE_REF_LINK_SOURCE)


# Link sources that mean "this PR opened the card". They decide whether the PR may rewrite
# the card's text and whether supersession may tell it that it was created for the PR, so
# losing one strands a card in the QA queue with nothing driving it.
_PR_TASK_PENDING_LINK_SOURCE = "pr_task_pending"
_PR_TASK_CREATED_LINK_SOURCE = "pr_task_created"
_PR_OWNED_LINK_SOURCES = (_PR_TASK_PENDING_LINK_SOURCE, _PR_TASK_CREATED_LINK_SOURCE)


# What may bring a superseded card back: a closing keyword a person WROTE, naming that
# card's issue again. Everything else -- a fuzzy match, a branch convention, a pending
# reservation -- is the automation guessing, and the card has already been told in a
# comment that it will receive no further updates.
_UN_SUPERSEDING_LINK_SOURCES = ("issue_keyword", _TITLE_REF_LINK_SOURCE)


def _stays_superseded(existing: str, incoming: str) -> bool:
    """True when a retired card must stay retired despite this link.

    Supersession is permanent to `revive_pr_links`, `mark_pr_merged` and
    `distinct_task_ids_for_pr`, and it was not to this one: reopening a PR re-runs the
    fuzzy pipeline, whose upsert cleared withdrawn_at and relabelled the row, resurrecting
    a card that had been told its work moved elsewhere -- and erasing the origin the text
    sync depends on. Only the author naming that issue again undoes it.
    """
    return existing == _SUPERSEDED_LINK_SOURCE and incoming not in _UN_SUPERSEDING_LINK_SOURCES


def _declines_repoint(existing: str, incoming: str, *, same_task: bool) -> bool:
    """True when this upsert would move a PR-owned row onto a different card.

    The row for a card THIS PR opened is the only reference to that card. Letting the
    fuzzy pipeline repoint it -- which happens when a reopened PR matches some other,
    pre-existing card -- leaves the created card referenced by nothing at all: no
    supersession notice, no rewind out of the QA queue, just a card with a QA assigned and
    nothing left that can move it. A guess does not get to do that; an explicit issue link
    supersedes properly, through `reconcile_pr_issue_links`.
    """
    return (
        not same_task
        and existing in _PR_OWNED_LINK_SOURCES
        and incoming not in _PR_OWNED_LINK_SOURCES
    )


def _kept_link_source(existing: str, incoming: str, *, same_task: bool) -> str:
    """Which of the two records what actually links this PR to that card.

    Reopening a PR re-runs the fuzzy pipeline, and its upsert carried the pipeline's own
    decision -- so a card the PR had CREATED came back as `auto_link`, after which its text
    stopped syncing and a later relink retired it as somebody else's, stranded in the QA
    queue. How a card came to exist does not change when a PR is reopened, so the origin
    wins.

    Only for the SAME card, though. When the row is repointed -- the fuzzy pipeline
    matching a different, pre-existing card on reopen -- carrying `pr_task_created` across
    would tell every later event that this PR opened somebody else's card: its title and
    description would be overwritten from the PR, and a relink would post "this card was
    created for the PR" and pull it out of the QA queue.
    """
    if same_task and existing in _PR_OWNED_LINK_SOURCES and incoming not in _PR_OWNED_LINK_SOURCES:
        return existing
    return incoming


async def upsert_pr_task_link(
    session: AsyncSession,
    *,
    github_repo: str,
    github_pr_number: int,
    plaky_task_id: str,
    github_issue_number: int,
    link_source: str,
) -> PullRequestTaskLink:
    """One row per (repo, pr, issue_key). `github_issue_number=0` means fuzzy / non-issue link."""
    q = select(PullRequestTaskLink).where(
        PullRequestTaskLink.github_repo == github_repo,
        PullRequestTaskLink.github_pr_number == github_pr_number,
        PullRequestTaskLink.github_issue_number == github_issue_number,
    )
    r = await session.execute(q)
    row = r.scalar_one_or_none()
    if row:
        same_task = str(row.plaky_task_id or "") == str(plaky_task_id)
        if _stays_superseded(str(row.link_source or ""), link_source) or _declines_repoint(
            str(row.link_source or ""), link_source, same_task=same_task
        ):
            return row
        row.link_source = _kept_link_source(
            str(row.link_source or ""), link_source, same_task=same_task
        )
        row.plaky_task_id = plaky_task_id
        row.merged_at = None
        row.withdrawn_at = None
        return row
    row = PullRequestTaskLink(
        github_repo=github_repo,
        github_pr_number=github_pr_number,
        plaky_task_id=plaky_task_id,
        github_issue_number=github_issue_number,
        link_source=link_source,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
        return row
    except IntegrityError:
        existing = await session.execute(q)
        row = existing.scalar_one_or_none()
        if row is None:
            raise
        # Same rules as the read-then-update path above: a race that lands here must not
        # resurrect a superseded card, forget which PR opened one, or repoint it.
        same_task = str(row.plaky_task_id or "") == str(plaky_task_id)
        if _stays_superseded(str(row.link_source or ""), link_source) or _declines_repoint(
            str(row.link_source or ""), link_source, same_task=same_task
        ):
            return row
        row.link_source = _kept_link_source(
            str(row.link_source or ""), link_source, same_task=same_task
        )
        row.plaky_task_id = plaky_task_id
        row.merged_at = None
        row.withdrawn_at = None
        return row


async def mark_pr_withdrawn(
    session: AsyncSession,
    *,
    github_repo: str,
    github_pr_number: int,
    github_updated_at: str = "",
) -> list[PullRequestTaskLink]:
    """PR closed without merge — exclude from merge-gated completion.

    `github_updated_at` is the PR's `updated_at` on the closing delivery. It is stored
    beside the local timestamp because only it can be compared with another delivery's
    `updated_at`: both come from GitHub's clock, while `withdrawn_at` records when this
    process happened to handle the close.
    """
    q = select(PullRequestTaskLink).where(
        PullRequestTaskLink.github_repo == github_repo,
        PullRequestTaskLink.github_pr_number == github_pr_number,
        PullRequestTaskLink.withdrawn_at.is_(None),
    )
    r = await session.execute(q)
    now = datetime.utcnow()
    stamp = (github_updated_at or "").strip()
    incoming = _github_instant(stamp)
    rows: list[PullRequestTaskLink] = []
    for row in r.scalars():
        already = _github_instant(row.withdrawn_github_at or "")
        if incoming is not None and already is not None and incoming <= already:
            # This close is not newer than the one already applied to a link that has since
            # been revived, so it is a redelivery or an out-of-order arrival. Re-withdrawing
            # on it leaves an OPEN PR reading as withdrawn -- the mirror image of the case
            # the revive side guards, and nothing withdraws or revives a second time.
            continue
        row.withdrawn_at = now
        row.withdrawn_github_at = stamp or None
        rows.append(row)
    return rows


def _github_instant(value: str) -> datetime | None:
    """One of GitHub's ISO-8601 timestamps as a naive UTC datetime, or None."""
    raw = (value or "").strip().replace("Z", "+00:00")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed


def _payload_is_older_than(withdrawn_github_at: str | None, updated_at: str) -> bool:
    """True when the delivery was built before the close that withdrew this link.

    GitHub stamps `state` and `closed_at` together, so a delivery built BEFORE a close --
    a webhook retry, a job the queue held, an event the poller read late -- says "open"
    with `closed_at` null and passes every field test there is. Only the timestamps order
    the two, and reviving on a stale one is permanent: nothing withdraws those links a
    second time.

    BOTH sides come from GitHub. Comparing against `withdrawn_at` instead would compare
    GitHub's clock with this host's, and would ask when this process handled the close
    rather than when the close happened -- a queued job or a poller catch-up puts those
    an hour apart, and a genuine reopen in between would be refused for good.

    A missing or unparsable timestamp on either side is not treated as stale. Some payload
    shapes (the events feed's slim pull_request) omit `updated_at`, and rows withdrawn
    before this column existed have nothing on the other side; refusing to revive those
    would strand the reopened PRs this path exists to rescue.
    """
    closed = _github_instant(withdrawn_github_at or "")
    built = _github_instant(updated_at)
    if closed is None or built is None:
        return False
    return built < closed


async def revive_pr_links(
    session: AsyncSession,
    *,
    github_repo: str,
    github_pr_number: int,
    not_before: str = "",
) -> list[PullRequestTaskLink]:
    """Un-withdraw a reopened PR's links, EXCEPT ones superseded by a real issue link.

    Closing a PR without merging withdraws its links, and `upsert_pr_task_link` is the
    only other thing that clears the flag -- which the ambiguous-triage short-circuit in
    `handle_pr_opened` never reaches. Without this, reopening a PR left every review,
    comment and push event resolving to no task at all.

    A link retired because the PR now points at an issue's task is deliberately left
    retired: reopening the PR does not un-say which issue it closes.

    `not_before` is the delivery's `updated_at`. A row withdrawn AFTER the delivery was
    built is left alone -- see `_payload_is_older_than`.
    """
    q = select(PullRequestTaskLink).where(
        PullRequestTaskLink.github_repo == github_repo,
        PullRequestTaskLink.github_pr_number == github_pr_number,
        PullRequestTaskLink.withdrawn_at.is_not(None),
        PullRequestTaskLink.link_source != _SUPERSEDED_LINK_SOURCE,
    )
    rows = [
        row
        for row in (await session.execute(q)).scalars()
        if not _payload_is_older_than(row.withdrawn_github_at, not_before)
    ]
    for row in rows:
        row.withdrawn_at = None
    return rows


async def mark_pr_merged(
    session: AsyncSession,
    *,
    github_repo: str,
    github_pr_number: int,
) -> list[PullRequestTaskLink]:
    """Mark all registry rows for this PR as merged (may span multiple issues/tasks)."""
    # Superseded rows only, not every withdrawn one. A card retired because the PR now
    # points at an issue's task has been told it will receive no further updates, so
    # merging must not flip it to Completed. A row withdrawn merely because the PR closed
    # is a different thing: if the reopen was missed -- a lost delivery, or a poller
    # restart, whose closed-PR memory is in-process -- excluding it would leave the task
    # stuck forever when the PR finally merges.
    q = select(PullRequestTaskLink).where(
        PullRequestTaskLink.github_repo == github_repo,
        PullRequestTaskLink.github_pr_number == github_pr_number,
        PullRequestTaskLink.merged_at.is_(None),
        PullRequestTaskLink.link_source != _SUPERSEDED_LINK_SOURCE,
    )
    r = await session.execute(q)
    rows = list(r.scalars())
    now = datetime.utcnow()
    for row in rows:
        row.merged_at = now
    return rows


async def withdrawn_pr_numbers(session: AsyncSession, *, github_repo: str) -> set[int]:
    """Every PR in this repo still holding links a close retired.

    The durable answer to "did this PR reopen while we were not watching". The poller's own
    memory of which PRs it saw close is in-process, so a restart loses it and a PR reopened
    in the meantime emits nothing at all -- its links stay withdrawn, and
    `has_any_open_pr_for_task` then lets merge-gated completion finish the task while that
    PR is still open. Superseded rows are excluded: those were retired on purpose.

    Asked once per repo per poll cycle, because a per-PR form opened a database session for
    every open PR, which under `TESTING_LIVE_PLAKY_REPOS=all` is hundreds a cycle.
    """
    q = select(PullRequestTaskLink.github_pr_number).where(
        PullRequestTaskLink.github_repo == github_repo,
        PullRequestTaskLink.withdrawn_at.is_not(None),
        PullRequestTaskLink.link_source != _SUPERSEDED_LINK_SOURCE,
    )
    return {int(n) for n in (await session.execute(q)).scalars()}


async def task_ids_for_open_pr(
    session: AsyncSession,
    *,
    github_repo: str,
    github_pr_number: int,
) -> list[str]:
    q = select(PullRequestTaskLink.plaky_task_id).where(
        PullRequestTaskLink.github_repo == github_repo,
        PullRequestTaskLink.github_pr_number == github_pr_number,
        _active_link_clause(),
    )
    r = await session.execute(q)
    return [str(x) for x in r.scalars().all()]


def _active_link_clause():
    return and_(
        PullRequestTaskLink.merged_at.is_(None),
        PullRequestTaskLink.withdrawn_at.is_(None),
    )


async def has_other_open_pr_for_task(
    session: AsyncSession,
    *,
    plaky_task_id: str,
    github_repo: str,
    current_pr_number: int,
) -> bool:
    q = select(PullRequestTaskLink.id).where(
        PullRequestTaskLink.plaky_task_id == plaky_task_id,
        PullRequestTaskLink.github_repo == github_repo,
        PullRequestTaskLink.github_pr_number != current_pr_number,
        _active_link_clause(),
    )
    r = await session.execute(q.limit(1))
    return r.scalar_one_or_none() is not None


async def has_any_open_pr_for_task(
    session: AsyncSession,
    *,
    plaky_task_id: str,
) -> bool:
    q = select(PullRequestTaskLink.id).where(
        PullRequestTaskLink.plaky_task_id == plaky_task_id,
        _active_link_clause(),
    )
    r = await session.execute(q.limit(1))
    return r.scalar_one_or_none() is not None


async def distinct_task_ids_for_pr(
    session: AsyncSession,
    *,
    github_repo: str,
    github_pr_number: int,
) -> list[str]:
    # Filters on the link SOURCE, not on withdrawn_at, because the two withdrawal
    # reasons mean different things:
    #
    #   superseded   the PR now names an issue whose task owns this work. Permanent. The
    #                retired card has been told it will receive no further updates, and
    #                it must stop receiving them -- otherwise one PR drives two cards.
    #   PR closed    provisional. The PR can come back, and `reopened` is the only event
    #                that clears it: a lost delivery or a poller restart (its closed-PR
    #                memory is in-process) would otherwise strand the PR forever, with
    #                reviews, comments and labels resolving to nothing.
    #
    # Callers that specifically want only-live links have `task_ids_for_open_pr`.
    # merged_at is not filtered either: a merged PR's task is still the task it drove.
    q = select(PullRequestTaskLink.plaky_task_id).where(
        PullRequestTaskLink.github_repo == github_repo,
        PullRequestTaskLink.github_pr_number == github_pr_number,
        PullRequestTaskLink.link_source != _SUPERSEDED_LINK_SOURCE,
    )
    r = await session.execute(q)
    seen: set[str] = set()
    out: list[str] = []
    for tid in r.scalars().all():
        s = str(tid)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _login_set(payload_users: Iterable[dict] | None) -> set[str]:
    out: set[str] = set()
    if not payload_users:
        return out
    for u in payload_users:
        if isinstance(u, dict):
            lg = u.get("login")
            if isinstance(lg, str) and lg.strip():
                out.add(lg.strip().casefold())
    return out


def pr_assignee_and_reviewer_logins(pr: dict) -> set[str]:
    """GitHub pull_request object assignees + requested_reviewers logins (lowercased)."""
    assignees = pr.get("assignees") if isinstance(pr, dict) else None
    reviewers = pr.get("requested_reviewers") if isinstance(pr, dict) else None
    return _login_set(assignees) | _login_set(reviewers)
