from __future__ import annotations

import logging
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from boardman.planning.datetime_utils import parse_iso_datetime
from boardman.planning.huddle.team_repos import load_team_repos, repos_for_team
from boardman.settings import settings

log = logging.getLogger(__name__)

LARGE_FILE_COUNT = 30
LARGE_LINE_DELTA = 2000


@dataclass(slots=True)
class PullRequestSummary:
    repo: str
    number: int
    title: str
    state: str
    merged: bool
    draft: bool
    author: str
    url: str
    additions: int
    deletions: int
    changed_files: int
    updated_at: str
    created_at: str
    labels: list[str]

    @property
    def is_large(self) -> bool:
        return (
            self.changed_files >= LARGE_FILE_COUNT
            or (self.additions + self.deletions) >= LARGE_LINE_DELTA
        )


def _github_owner() -> str:
    return (settings.github_bare_repo_owner or settings.github_org or "Team-Deepiri").strip()


class GitHubPlanningContext:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client
        self._team_repos = load_team_repos()

    def enabled(self) -> bool:
        return bool(settings.github_pat)

    def fetch_recent_prs(self, team_focus: str) -> list[PullRequestSummary]:
        if not self.enabled():
            return []
        repos = repos_for_team(self._team_repos, team_focus)
        if not repos:
            log.info("planning_github_no_repos team_focus=%r", team_focus)
            return []
        cutoff = datetime.now(UTC) - timedelta(days=settings.planning_github_lookback_days)
        summaries: list[PullRequestSummary] = []
        with self._open_client() as client:
            for repo in repos:
                try:
                    summaries.extend(self._fetch_repo_prs(client, repo, cutoff))
                except httpx.HTTPError as exc:
                    log.warning(
                        "planning_github_repo_failed repo=%s error_type=%s error=%s",
                        repo,
                        type(exc).__name__,
                        str(exc)[:400],
                    )
        summaries.sort(key=lambda item: item.updated_at, reverse=True)
        return summaries

    def context_markdown(self, team_focus: str) -> str:
        if not self.enabled():
            return "GitHub not configured (set GITHUB_PAT)."
        prs = self.fetch_recent_prs(team_focus)
        lookback = settings.planning_github_lookback_days
        if not prs:
            repos = repos_for_team(self._team_repos, team_focus)
            if not repos:
                return (
                    "No GitHub repos mapped for this team. "
                    f"Edit {settings.planning_team_repos_file}."
                )
            return f"No pull requests updated in the last {lookback} days for team repos."
        return self._format_markdown(prs, team_focus, lookback)

    def _open_client(self) -> AbstractContextManager[httpx.Client]:
        # An injected client is borrowed: hand it back via ``nullcontext`` so the
        # ``with`` block does not close a client the caller still owns. Only a
        # client we create ourselves is closed on exit.
        if self._client is not None:
            return nullcontext(self._client)
        return httpx.Client(
            timeout=settings.planning_llm_timeout_seconds,
            headers={
                "Authorization": f"Bearer {settings.github_pat}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def _fetch_repo_prs(
        self,
        client: httpx.Client,
        repo: str,
        cutoff: datetime,
    ) -> list[PullRequestSummary]:
        org = _github_owner()
        page = 1
        found: list[PullRequestSummary] = []
        while page <= 10:
            response = client.get(
                f"https://api.github.com/repos/{org}/{repo}/pulls",
                params={
                    "state": "all",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": 100,
                    "page": page,
                },
            )
            response.raise_for_status()
            batch = response.json()
            if not batch:
                break
            stop = False
            for item in batch:
                updated = parse_iso_datetime(item.get("updated_at"))
                if updated is None or updated < cutoff:
                    stop = True
                    continue
                if settings.planning_github_skip_bots and _is_bot_author(item):
                    continue
                found.append(_to_summary(repo, item))
            if stop or len(batch) < 100:
                break
            page += 1
        return found

    @staticmethod
    def _format_markdown(
        prs: list[PullRequestSummary],
        team_focus: str,
        lookback_days: int,
    ) -> str:
        merged = [p for p in prs if p.merged]
        open_prs = [p for p in prs if p.state == "open"]
        closed_unmerged = [p for p in prs if p.state == "closed" and not p.merged]

        lines = [
            f"## GitHub Pull Requests (last {lookback_days} days)",
            f"- Team focus: {team_focus}",
            f"- Total PRs with activity: {len(prs)}",
            "",
        ]
        lines.extend(_section("Merged", merged))
        lines.extend(_section("Open", open_prs))
        lines.extend(_section("Closed (not merged)", closed_unmerged))
        lines.append("- Note: stats only (no diffs). LARGE = many files or line changes.")
        return "\n".join(lines)


def _section(title: str, prs: list[PullRequestSummary]) -> list[str]:
    if not prs:
        return [f"### {title}", "- None", ""]
    lines = [f"### {title}"]
    for pr in prs[:25]:
        labels = ", ".join(pr.labels[:5]) if pr.labels else "none"
        size = f"+{pr.additions}/-{pr.deletions}, {pr.changed_files} files"
        large = " LARGE" if pr.is_large else ""
        status = "merged" if pr.merged else pr.state
        lines.append(
            f"- [{pr.repo}#{pr.number}]({pr.url}) — {pr.title} — "
            f"{status} — {size}{large} — @{pr.author} — labels: {labels}"
        )
    if len(prs) > 25:
        lines.append(f"- … and {len(prs) - 25} more")
    lines.append("")
    return lines


def _to_summary(repo: str, item: dict) -> PullRequestSummary:
    user = item.get("user") or {}
    labels = [label.get("name", "") for label in item.get("labels", []) if label.get("name")]
    merged_at = item.get("merged_at")
    return PullRequestSummary(
        repo=repo,
        number=int(item["number"]),
        title=str(item.get("title") or "").strip(),
        state=str(item.get("state") or "unknown"),
        merged=merged_at is not None,
        draft=bool(item.get("draft")),
        author=str(user.get("login") or "unknown"),
        url=str(item.get("html_url") or ""),
        additions=int(item.get("additions") or 0),
        deletions=int(item.get("deletions") or 0),
        changed_files=int(item.get("changed_files") or 0),
        updated_at=str(item.get("updated_at") or ""),
        created_at=str(item.get("created_at") or ""),
        labels=labels,
    )


def _is_bot_author(item: dict) -> bool:
    user = item.get("user") or {}
    login = str(user.get("login") or "").lower()
    return login.endswith("[bot]") or "dependabot" in login or "renovate" in login
