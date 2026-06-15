from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from boardman.database.models import ProjectContext, ScanRun
from boardman.database.session import async_session
from boardman.github.repo_fetch import fetch_direction_md
from boardman.planning.context_sync import full_repo_name, repo_matches_team
from boardman.planning.team_repos import load_team_repos, repos_for_team
from boardman.settings import settings

log = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]
DirectionFetcher = Callable[[str], Awaitable[str]]


@dataclass(slots=True)
class RepoDirectionSummary:
    repo_full: str
    excerpt: str
    source: str  # cache | github | scan_only


@dataclass(slots=True)
class ScanRunSummary:
    repo_full: str
    tasks_proposed: int
    tasks_created: int
    created_at: str
    error: str | None


class DirectionPlanningContext:
    """Markdown context from DIRECTION.md, cached ProjectContext, and recent scan runs."""

    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        team_repos: dict[str, list[str]] | None = None,
        direction_fetcher: DirectionFetcher | None = None,
    ) -> None:
        self._session_factory = session_factory or async_session
        self._team_repos = team_repos or load_team_repos()
        self._direction_fetcher = direction_fetcher or _default_direction_fetcher

    def context_markdown(self, team_focus: str) -> str:
        return asyncio.run(self._context_markdown_async(team_focus))

    async def _context_markdown_async(self, team_focus: str) -> str:
        team_repo_names = repos_for_team(self._team_repos, team_focus)
        if not team_repo_names:
            return (
                "No GitHub repos mapped for this team in direction context. "
                f"Edit {settings.planning_team_repos_file}."
            )
        repo_full_names = [full_repo_name(name) for name in team_repo_names]
        directions = await self._resolve_directions(repo_full_names)
        scans = await self._fetch_recent_scans(team_repo_names)
        if not directions and not scans:
            return "No repo direction or scan history available for this team."
        return self._format_markdown(team_focus, directions, scans)

    async def _resolve_directions(
        self,
        repo_full_names: list[str],
    ) -> list[RepoDirectionSummary]:
        cache_hours = settings.planning_direction_cache_hours
        excerpt_limit = settings.planning_direction_excerpt_chars
        summaries: list[RepoDirectionSummary] = []
        async with self._session_factory() as session:
            for repo_full in repo_full_names:
                cached = await self._load_project_context(session, repo_full)
                if cached is not None and _cache_fresh(cached.last_scanned, cache_hours):
                    excerpt = _excerpt(cached.summary or "", excerpt_limit)
                    if excerpt:
                        summaries.append(
                            RepoDirectionSummary(
                                repo_full=repo_full,
                                excerpt=excerpt,
                                source="cache",
                            )
                        )
                        continue
                if settings.github_pat:
                    try:
                        text = await self._direction_fetcher(repo_full)
                    except Exception as exc:
                        log.warning(
                            "planning_direction_fetch_failed repo=%s error_type=%s error=%s",
                            repo_full,
                            type(exc).__name__,
                            str(exc)[:400],
                        )
                        text = ""
                    excerpt = _excerpt(text, excerpt_limit)
                    if excerpt and not excerpt.startswith("("):
                        summaries.append(
                            RepoDirectionSummary(
                                repo_full=repo_full,
                                excerpt=excerpt,
                                source="github",
                            )
                        )
                        continue
                if cached is not None and cached.summary:
                    excerpt = _excerpt(cached.summary, excerpt_limit)
                    if excerpt:
                        summaries.append(
                            RepoDirectionSummary(
                                repo_full=repo_full,
                                excerpt=excerpt,
                                source="cache_stale",
                            )
                        )
        return summaries

    async def _load_project_context(
        self,
        session: AsyncSession,
        repo_full: str,
    ) -> ProjectContext | None:
        result = await session.execute(
            select(ProjectContext).where(ProjectContext.repo == repo_full)
        )
        return result.scalar_one_or_none()

    async def _fetch_recent_scans(self, team_repo_names: list[str]) -> list[ScanRunSummary]:
        lookback = timedelta(days=settings.planning_github_lookback_days)
        cutoff = datetime.utcnow() - lookback
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(ScanRun)
                    .where(ScanRun.created_at >= cutoff)
                    .order_by(ScanRun.created_at.desc())
                )
            ).scalars().all()
        summaries: list[ScanRunSummary] = []
        seen: set[str] = set()
        for row in rows:
            if not repo_matches_team(row.github_repo, team_repo_names):
                continue
            if row.github_repo in seen:
                continue
            seen.add(row.github_repo)
            proposed = _count_proposed_tasks(row.tasks_proposed)
            summaries.append(
                ScanRunSummary(
                    repo_full=row.github_repo,
                    tasks_proposed=proposed,
                    tasks_created=row.tasks_created,
                    created_at=row.created_at.isoformat() if row.created_at else "",
                    error=row.error,
                )
            )
        return summaries

    @staticmethod
    def _format_markdown(
        team_focus: str,
        directions: list[RepoDirectionSummary],
        scans: list[ScanRunSummary],
    ) -> str:
        lines = [
            "## Repo Direction",
            f"- Team focus: {team_focus}",
            "",
        ]
        if directions:
            lines.append("### DIRECTION summaries")
            for row in directions:
                lines.append(f"- **{row.repo_full}** ({row.source}): {row.excerpt}")
            lines.append("")
        else:
            lines.extend(["### DIRECTION summaries", "- None", ""])
        if scans:
            lines.append("### Recent AI scans")
            for row in scans:
                err = f" — error: {row.error[:120]}" if row.error else ""
                lines.append(
                    f"- {row.repo_full}: proposed {row.tasks_proposed} tasks, "
                    f"created {row.tasks_created} — scanned {row.created_at[:10]}{err}"
                )
            lines.append("")
        else:
            lines.extend(["### Recent AI scans", "- None", ""])
        return "\n".join(lines).strip()


async def _default_direction_fetcher(repo_full: str) -> str:
    if "/" not in repo_full:
        return ""
    owner, repo = repo_full.split("/", 1)
    async with httpx.AsyncClient(timeout=settings.planning_llm_timeout_seconds) as client:
        return await fetch_direction_md(client, owner, repo)


def _cache_fresh(last_scanned: datetime | None, cache_hours: int) -> bool:
    if last_scanned is None:
        return False
    age = datetime.utcnow() - last_scanned
    return age <= timedelta(hours=cache_hours)


def _excerpt(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    if not cleaned:
        return ""
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _count_proposed_tasks(tasks_proposed: str | None) -> int:
    if not tasks_proposed:
        return 0
    try:
        parsed = json.loads(tasks_proposed)
    except json.JSONDecodeError:
        return 0
    if isinstance(parsed, list):
        return len(parsed)
    return 0
