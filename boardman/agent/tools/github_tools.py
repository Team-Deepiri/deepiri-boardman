"""GitHub read-only helpers for the agent."""

from __future__ import annotations

import json
from typing import List, Optional

import httpx
from langchain_core.tools import StructuredTool

from boardman.github.repo_fetch import (
    fetch_default_branch,
    fetch_direction_md,
    fetch_open_issues,
    fetch_readme_md,
    fetch_recent_commits,
    fetch_repo_file_text,
    fetch_repo_overview,
    parse_owner_repo,
)
from boardman.repos_config import list_workspace_repos
from boardman.settings import settings


async def _github_list_workspace_repos() -> str:
    """List all GitHub repositories in the configured org merged with repos.yml config."""
    if not settings.github_pat:
        return json.dumps({"ok": False, "message": "GITHUB_PAT not configured"})
    async with httpx.AsyncClient(timeout=30.0) as client:
        repos = await list_workspace_repos(client)
    # Convert RepoRouting objects to dicts for JSON
    out = {
        name: {
            "category": r.category,
            "plaky_table": r.plaky_table,
            "plaky_board_id": r.plaky_board_id,
            "plaky_group_id": r.plaky_group_id,
            "description": r.description,
        }
        for name, r in repos.items()
    }
    return json.dumps({"ok": True, "repos": out})


async def _github_list_open_issues(owner_repo: str) -> str:
    if not settings.github_pat:
        return json.dumps({"ok": False, "message": "GITHUB_PAT not configured"})
    parsed = parse_owner_repo(owner_repo)
    if not parsed:
        return json.dumps({"ok": False, "message": "owner_repo must be owner/name"})
    owner, repo = parsed
    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = {"Authorization": f"Bearer {settings.github_pat}", "Accept": "application/vnd.github+json"}
        r = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/issues?state=open&per_page=30",
            headers=headers,
        )
        if r.status_code != 200:
            return json.dumps({"ok": False, "status": r.status_code, "text": r.text[:500]})
        issues = r.json()
        slim = [
            {"number": i["number"], "title": i.get("title"), "url": i.get("html_url")}
            for i in issues
            if isinstance(i, dict) and "pull_request" not in i
        ]
        return json.dumps({"ok": True, "issues": slim})


async def _github_fetch_direction(owner_repo: str) -> str:
    """Load DIRECTION.md from default branch (main/master fallback inside fetch_direction_md)."""
    if not settings.github_pat:
        return json.dumps({"ok": False, "message": "GITHUB_PAT not configured"})
    parsed = parse_owner_repo(owner_repo)
    if not parsed:
        return json.dumps({"ok": False, "message": "owner_repo must be owner/name"})
    owner, repo = parsed
    async with httpx.AsyncClient(timeout=45.0) as client:
        text = await fetch_direction_md(client, owner, repo)
    return json.dumps({"ok": True, "owner": owner, "repo": repo, "DIRECTION_md": text}, default=str)[:14000]


async def _github_fetch_file(owner_repo: str, path: str, ref: str = "") -> str:
    """Read a single text file from the repo (e.g. README.md, docs/spec.md)."""
    if not settings.github_pat:
        return json.dumps({"ok": False, "message": "GITHUB_PAT not configured"})
    parsed = parse_owner_repo(owner_repo)
    if not parsed:
        return json.dumps({"ok": False, "message": "owner_repo must be owner/name"})
    owner, repo = parsed
    async with httpx.AsyncClient(timeout=45.0) as client:
        branch = (ref or "").strip()
        if not branch:
            branch = await fetch_default_branch(client, owner, repo)
        text = await fetch_repo_file_text(client, owner, repo, path.strip(), ref=branch)
    return json.dumps({"ok": True, "path": path, "ref": branch, "content": text}, default=str)[:14000]


def _looks_missing(text: str) -> bool:
    """Content fetchers signal errors in-band as '(...)' strings."""
    s = (text or "").strip()
    return not s or (s.startswith("(") and s.endswith(")"))


async def _workspace_repo_suggestions(client: httpx.AsyncClient, requested: str, limit: int = 5) -> list[str]:
    """Closest workspace repos to a requested name (users say 'deepiri-cyrex' for 'diri-cyrex')."""
    from difflib import SequenceMatcher

    from boardman.github.org_repos import fetch_org_repository_full_names

    try:
        names = await fetch_org_repository_full_names(client, settings.github_org)
    except Exception:
        return []
    want = (requested or "").split("/")[-1].strip().lower()
    if not want or not names:
        return []
    scored: list[tuple[float, str]] = []
    for fn in names:
        short = fn.split("/")[-1].lower()
        score = SequenceMatcher(None, want, short).ratio()
        if want in short or short in want:
            score += 0.3
        scored.append((score, fn))
    scored.sort(key=lambda t: -t[0])
    return [fn for score, fn in scored[:limit] if score >= 0.45]


async def _github_repo_planning_context(owner_repo: str, commits_limit: int = 20) -> str:
    """
    One call: DIRECTION.md + README + structural overview + recent commits + open issues.
    Layered so a repo with NO markdown files still yields real context: description,
    topics, language mix, file-tree summary, manifests, entry points.
    """
    if not settings.github_pat:
        return json.dumps({"ok": False, "message": "GITHUB_PAT not configured"})
    raw = (owner_repo or "").strip()
    parsed = parse_owner_repo(raw)
    if not parsed and raw and "/" not in raw:
        # Bare name: assume the configured default owner instead of erroring out.
        from boardman.assignment.qa_picker import ensure_github_owner_repo

        parsed = parse_owner_repo(ensure_github_owner_repo(raw))
    if not parsed:
        return json.dumps({"ok": False, "message": "owner_repo must be owner/name"})
    owner, repo = parsed
    lim = max(5, min(int(commits_limit) if commits_limit else 20, 50))
    import asyncio

    async with httpx.AsyncClient(timeout=90.0) as client:
        overview = await fetch_repo_overview(client, owner, repo)
        if isinstance(overview, dict) and "error" in overview:
            # Repo not found / inaccessible — help the model self-correct instead of
            # letting it conclude the codebase is a blank slate.
            suggestions = await _workspace_repo_suggestions(client, repo)
            return json.dumps(
                {
                    "ok": False,
                    "repo": f"{owner}/{repo}",
                    "repo_not_found": True,
                    "message": (
                        f"GitHub repo {owner}/{repo} does not exist or is inaccessible "
                        f"({overview.get('error')}). Do NOT invent an analysis for it."
                    ),
                    "did_you_mean": suggestions,
                    "guidance": (
                        "If one of did_you_mean matches the user's intent, call this tool again "
                        "with that exact owner/repo. Otherwise ask the user to confirm the name "
                        "(you can also call github_list_workspace_repos)."
                    ),
                }
            )
        direction, readme, commits, issues = await asyncio.gather(
            fetch_direction_md(client, owner, repo),
            fetch_readme_md(client, owner, repo),
            fetch_recent_commits(client, owner, repo, limit=lim),
            fetch_open_issues(client, owner, repo),
        )

        has_direction = not _looks_missing(direction)
        has_readme = not _looks_missing(readme)

        manifest_excerpts: dict[str, str] = {}
        if not has_direction and not has_readme:
            # No docs at all — pull the top manifests so purpose/deps are still visible.
            branch = overview.get("default_branch", "") if isinstance(overview, dict) else ""
            for path in (overview.get("manifests") or [])[:3] if isinstance(overview, dict) else []:
                text = await fetch_repo_file_text(client, owner, repo, path, ref=branch)
                manifest_excerpts[path] = text[:2500]

    sources = {
        "DIRECTION_md": "found" if has_direction else "missing",
        "README": "found" if has_readme else "missing",
        "structure_overview": "found" if isinstance(overview, dict) and "error" not in overview else "unavailable",
        "recent_commits": "found" if not _looks_missing(commits) else "unavailable",
        "open_issues": "found" if not _looks_missing(issues) else "unavailable",
    }
    guidance = ""
    if not has_direction and not has_readme:
        guidance = (
            "No DIRECTION.md or README in this repo. Do NOT give up: explain the repo from "
            "structure_overview (description, topics, languages, top_level dirs, entry_points) "
            "and manifest_excerpts, and use github_fetch_file to read entry-point/source files "
            "when more depth is needed. State clearly that the repo has no docs and which "
            "signals your explanation is based on."
        )
    elif not has_direction:
        guidance = "DIRECTION.md missing — ground on README + structure_overview; suggest `boardman init` to seed DIRECTION.md."

    out = {
        "ok": True,
        "repo": f"{owner}/{repo}",
        "context_sources": sources,
        "guidance": guidance,
        "DIRECTION_md": direction[:8000] if has_direction else direction,
        "README_md": readme[:8000] if has_readme else readme,
        "structure_overview": overview,
        "manifest_excerpts": manifest_excerpts,
        "recent_commits_markdown": commits[:4000],
        "open_issues_markdown": issues[:4000],
    }
    body = json.dumps(out, default=str)
    if len(body) > 24000:
        # Trim the biggest text fields instead of slicing JSON mid-token.
        for k in ("README_md", "DIRECTION_md"):
            if isinstance(out.get(k), str) and len(out[k]) > 4000:
                out[k] = out[k][:4000] + "\n…(truncated)"
        body = json.dumps(out, default=str)[:24000]
    return body


def github_list_workspace_repos_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_github_list_workspace_repos,
        name="github_list_workspace_repos",
        description=(
            "List all GitHub repositories in the configured org merged with repos.yml. "
            "Use this when you need to know which repos are available to the agent."
        ),
    )


def github_list_open_issues_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_github_list_open_issues,
        name="github_list_open_issues",
        description="List open GitHub issues (not PRs) for owner/repo (e.g. deepiri-org/boardman).",
    )


def github_fetch_direction_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_github_fetch_direction,
        name="github_fetch_direction",
        description=(
            "Fetch DIRECTION.md from GitHub for owner/repo (tries main then master). "
            "Requires GITHUB_PAT. Args: owner_repo (e.g. deepiri/emotion-desktop)."
        ),
    )


def github_fetch_file_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_github_fetch_file,
        name="github_fetch_file",
        description=(
            "Read a file from the GitHub repo (UTF-8 text). Args: owner_repo, path (e.g. README.md), "
            "optional ref (branch or tag; default = repo default branch)."
        ),
    )


def github_repo_planning_context_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_github_repo_planning_context,
        name="github_repo_planning_context",
        description=(
            "Bundle DIRECTION.md + README + structural overview (description, topics, languages, "
            "file tree, manifests, entry points) + recent commits + open issues for owner/repo in "
            "one call — ALWAYS the starting point when explaining or planning a remote GitHub repo, "
            "including repos with no markdown docs at all (the structure_overview and "
            "manifest_excerpts fields cover that case). Optional commits_limit (default 20, max 50). "
            "Requires GITHUB_PAT."
        ),
    )


def build_github_tools() -> List[StructuredTool]:
    return [
        github_list_workspace_repos_tool(),
        github_repo_planning_context_tool(),
        github_fetch_direction_tool(),
        github_fetch_file_tool(),
        github_list_open_issues_tool(),
    ]
