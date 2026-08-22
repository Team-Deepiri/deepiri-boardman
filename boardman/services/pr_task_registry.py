"""Track GitHub PR ↔ Plaky task links for multi-PR tasks and merge-gated completion."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

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
        row.plaky_task_id = plaky_task_id
        row.link_source = link_source
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
        row.plaky_task_id = plaky_task_id
        row.link_source = link_source
        row.merged_at = None
        row.withdrawn_at = None
        return row


async def mark_pr_withdrawn(
    session: AsyncSession,
    *,
    github_repo: str,
    github_pr_number: int,
) -> list[PullRequestTaskLink]:
    """PR closed without merge — exclude from merge-gated completion."""
    q = select(PullRequestTaskLink).where(
        PullRequestTaskLink.github_repo == github_repo,
        PullRequestTaskLink.github_pr_number == github_pr_number,
        PullRequestTaskLink.withdrawn_at.is_(None),
    )
    r = await session.execute(q)
    rows = list(r.scalars())
    now = datetime.utcnow()
    for row in rows:
        row.withdrawn_at = now
    return rows


async def revive_pr_links(
    session: AsyncSession,
    *,
    github_repo: str,
    github_pr_number: int,
) -> list[PullRequestTaskLink]:
    """Un-withdraw a reopened PR's links, EXCEPT ones superseded by a real issue link.

    Closing a PR without merging withdraws its links, and `upsert_pr_task_link` is the
    only other thing that clears the flag -- which the ambiguous-triage short-circuit in
    `handle_pr_opened` never reaches. Without this, reopening a PR left every review,
    comment and push event resolving to no task at all.

    A link retired because the PR now points at an issue's task is deliberately left
    retired: reopening the PR does not un-say which issue it closes.
    """
    q = select(PullRequestTaskLink).where(
        PullRequestTaskLink.github_repo == github_repo,
        PullRequestTaskLink.github_pr_number == github_pr_number,
        PullRequestTaskLink.withdrawn_at.is_not(None),
        PullRequestTaskLink.link_source != _SUPERSEDED_LINK_SOURCE,
    )
    rows = list((await session.execute(q)).scalars())
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
