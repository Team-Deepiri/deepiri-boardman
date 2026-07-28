from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from boardman.database.models import IssueTaskMap, OpenPRTrack, PullRequestTaskLink, SyncLog
from boardman.database.session import async_session
from boardman.planning.huddle.async_bridge import run_sync
from boardman.planning.huddle.team_repos import load_team_repos, repos_for_team
from boardman.settings import settings

log = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(slots=True)
class PRLinkSummary:
    repo: str
    pr_number: int
    plaky_task_id: str
    link_source: str
    merged: bool
    withdrawn: bool


@dataclass(slots=True)
class IssueMapSummary:
    repo: str
    issue_number: int
    plaky_task_id: str
    plaky_task_url: str | None


@dataclass(slots=True)
class OpenPRTrackSummary:
    repo_full_name: str
    pr_number: int
    plaky_item_id: str
    pr_title: str | None
    pr_url: str | None


@dataclass(slots=True)
class SyncActivitySummary:
    action: str
    count: int


def _github_owner() -> str:
    return (settings.github_bare_repo_owner or settings.github_org or "Team-Deepiri").strip()


def repo_matches_team(repo: str, team_repo_names: list[str]) -> bool:
    """Match bare repo slugs or owner/repo full names against team repo list."""
    if not repo or not team_repo_names:
        return False
    normalized = repo.strip().lower()
    bare = normalized.split("/")[-1]
    owner = _github_owner().lower()
    for name in team_repo_names:
        key = name.strip().lower()
        if not key:
            continue
        if normalized == key or bare == key:
            return True
        if normalized == f"{owner}/{key}":
            return True
    return False


def full_repo_name(bare_repo: str) -> str:
    bare = bare_repo.strip()
    if "/" in bare:
        return bare
    return f"{_github_owner()}/{bare}"


class SyncPlanningContext:
    """Markdown context from boardman SQLite sync state (PR links, issue maps, QA tracks)."""

    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        team_repos: dict[str, list[str]] | None = None,
    ) -> None:
        self._session_factory = session_factory or async_session
        self._team_repos = team_repos or load_team_repos()

    def context_markdown(self, team_focus: str) -> str:
        return run_sync(self._context_markdown_async(team_focus))

    async def _context_markdown_async(self, team_focus: str) -> str:
        team_repo_names = repos_for_team(self._team_repos, team_focus)
        if not team_repo_names:
            return (
                "No GitHub repos mapped for this team in boardman sync context. "
                f"Edit {settings.planning_team_repos_file}."
            )
        lookback = settings.planning_sync_lookback_days
        pr_links = await self._fetch_pr_links(team_repo_names)
        issue_maps = await self._fetch_issue_maps(team_repo_names)
        open_tracks = await self._fetch_open_pr_tracks(team_repo_names)
        activity = await self._fetch_sync_activity(team_repo_names, lookback)
        if not pr_links and not issue_maps and not open_tracks and not activity:
            return (
                f"No boardman sync history for this team's repos in the last {lookback} days."
            )
        return self._format_markdown(
            team_focus=team_focus,
            lookback_days=lookback,
            pr_links=pr_links,
            issue_maps=issue_maps,
            open_tracks=open_tracks,
            activity=activity,
        )

    async def _fetch_pr_links(self, team_repo_names: list[str]) -> list[PRLinkSummary]:
        async with self._session_factory() as session:
            rows = (await session.execute(select(PullRequestTaskLink))).scalars().all()
        summaries: list[PRLinkSummary] = []
        for row in rows:
            if not repo_matches_team(row.github_repo, team_repo_names):
                continue
            summaries.append(
                PRLinkSummary(
                    repo=row.github_repo,
                    pr_number=row.github_pr_number,
                    plaky_task_id=row.plaky_task_id,
                    link_source=row.link_source,
                    merged=row.merged_at is not None,
                    withdrawn=row.withdrawn_at is not None,
                )
            )
        summaries.sort(key=lambda item: (item.plaky_task_id, item.repo, item.pr_number))
        return summaries

    async def _fetch_issue_maps(self, team_repo_names: list[str]) -> list[IssueMapSummary]:
        async with self._session_factory() as session:
            rows = (await session.execute(select(IssueTaskMap))).scalars().all()
        summaries: list[IssueMapSummary] = []
        for row in rows:
            if not repo_matches_team(row.github_repo, team_repo_names):
                continue
            summaries.append(
                IssueMapSummary(
                    repo=row.github_repo,
                    issue_number=row.github_issue_number,
                    plaky_task_id=row.plaky_task_id,
                    plaky_task_url=row.plaky_task_url,
                )
            )
        summaries.sort(key=lambda item: (item.repo, item.issue_number))
        return summaries

    async def _fetch_open_pr_tracks(self, team_repo_names: list[str]) -> list[OpenPRTrackSummary]:
        async with self._session_factory() as session:
            rows = (await session.execute(select(OpenPRTrack))).scalars().all()
        summaries: list[OpenPRTrackSummary] = []
        for row in rows:
            if not repo_matches_team(row.repo_full_name, team_repo_names):
                continue
            summaries.append(
                OpenPRTrackSummary(
                    repo_full_name=row.repo_full_name,
                    pr_number=row.pr_number,
                    plaky_item_id=row.plaky_item_id,
                    pr_title=row.pr_title,
                    pr_url=row.pr_url,
                )
            )
        summaries.sort(key=lambda item: (item.repo_full_name, item.pr_number))
        return summaries

    async def _fetch_sync_activity(
        self,
        team_repo_names: list[str],
        lookback_days: int,
    ) -> list[SyncActivitySummary]:
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=lookback_days)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(SyncLog.action, func.count())
                    .where(SyncLog.created_at >= cutoff)
                    .group_by(SyncLog.action)
                )
            ).all()
        repo_filtered: dict[str, int] = defaultdict(int)
        async with self._session_factory() as session:
            recent = (
                await session.execute(
                    select(SyncLog).where(SyncLog.created_at >= cutoff)
                )
            ).scalars().all()
        for entry in recent:
            if entry.github_repo and repo_matches_team(entry.github_repo, team_repo_names):
                repo_filtered[entry.action] += 1
        if repo_filtered:
            return [
                SyncActivitySummary(action=action, count=count)
                for action, count in sorted(repo_filtered.items())
            ]
        # Fall back to global counts when repo tags are missing on older rows.
        return [
            SyncActivitySummary(action=str(action), count=int(count))
            for action, count in sorted(rows, key=lambda pair: str(pair[0]))
        ]

    @staticmethod
    def _format_markdown(
        *,
        team_focus: str,
        lookback_days: int,
        pr_links: list[PRLinkSummary],
        issue_maps: list[IssueMapSummary],
        open_tracks: list[OpenPRTrackSummary],
        activity: list[SyncActivitySummary],
    ) -> str:
        lines = [
            "## Boardman Sync State",
            f"- Team focus: {team_focus}",
            f"- Sync activity lookback: {lookback_days} days",
            "",
        ]
        lines.extend(_format_pr_links(pr_links))
        lines.extend(_format_issue_maps(issue_maps))
        lines.extend(_format_open_tracks(open_tracks))
        lines.extend(_format_activity(activity))
        return "\n".join(lines).strip()


def _format_pr_links(links: list[PRLinkSummary]) -> list[str]:
    if not links:
        return ["### PR ↔ Plaky task links", "- None", ""]
    grouped: dict[str, list[PRLinkSummary]] = defaultdict(list)
    for link in links:
        grouped[link.plaky_task_id].append(link)
    lines = ["### PR ↔ Plaky task links"]
    for task_id, group in sorted(grouped.items()):
        parts: list[str] = []
        for link in group:
            status = "merged" if link.merged else ("withdrawn" if link.withdrawn else "open")
            parts.append(f"{link.repo}#{link.pr_number} ({status}, {link.link_source})")
        lines.append(f"- Plaky `{task_id}`: " + "; ".join(parts))
    lines.append("")
    return lines


def _format_issue_maps(maps: list[IssueMapSummary]) -> list[str]:
    if not maps:
        return ["### Issue ↔ Plaky mappings", "- None", ""]
    lines = ["### Issue ↔ Plaky mappings"]
    for row in maps[:30]:
        url = f" — {row.plaky_task_url}" if row.plaky_task_url else ""
        lines.append(
            f"- {row.repo}#{row.issue_number} → Plaky `{row.plaky_task_id}`{url}"
        )
    if len(maps) > 30:
        lines.append(f"- … and {len(maps) - 30} more")
    lines.append("")
    return lines


def _format_open_tracks(tracks: list[OpenPRTrackSummary]) -> list[str]:
    if not tracks:
        return ["### Open PR tracks (QA pipeline)", "- None", ""]
    lines = ["### Open PR tracks (QA pipeline)"]
    for row in tracks[:25]:
        title = row.pr_title or "untitled"
        url = f" — {row.pr_url}" if row.pr_url else ""
        lines.append(
            f"- {row.repo_full_name}#{row.pr_number} — {title} — "
            f"Plaky `{row.plaky_item_id}`{url}"
        )
    if len(tracks) > 25:
        lines.append(f"- … and {len(tracks) - 25} more")
    lines.append("")
    return lines


def _format_activity(activity: list[SyncActivitySummary]) -> list[str]:
    if not activity:
        return ["### Recent webhook sync activity", "- None", ""]
    lines = ["### Recent webhook sync activity"]
    for row in activity:
        lines.append(f"- {row.action}: {row.count}")
    lines.append("")
    return lines
