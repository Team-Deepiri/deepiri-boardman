"""Track GitHub PR ↔ Plaky task links for multi-PR tasks and merge-gated completion."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import PullRequestTaskLink


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
    session.add(row)
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


async def mark_pr_merged(
    session: AsyncSession,
    *,
    github_repo: str,
    github_pr_number: int,
) -> list[PullRequestTaskLink]:
    """Mark all registry rows for this PR as merged (may span multiple issues/tasks)."""
    q = select(PullRequestTaskLink).where(
        PullRequestTaskLink.github_repo == github_repo,
        PullRequestTaskLink.github_pr_number == github_pr_number,
        PullRequestTaskLink.merged_at.is_(None),
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
    q = select(PullRequestTaskLink.plaky_task_id).where(
        PullRequestTaskLink.github_repo == github_repo,
        PullRequestTaskLink.github_pr_number == github_pr_number,
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
