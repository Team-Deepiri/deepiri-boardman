"""GitHub read-only helpers for the agent."""

from __future__ import annotations

import json

import httpx
from langchain_core.tools import StructuredTool

from boardman.github.repo_fetch import (
    fetch_default_branch,
    fetch_direction_md,
    fetch_open_issues,
    fetch_recent_commits,
    fetch_repo_file_text,
    parse_owner_repo,
)
from boardman.github.repo_metadata import fetch_repo_metadata
from boardman.repos_config import list_workspace_repos
from boardman.settings import settings

_NOTABLE_FILE_BASENAMES = {
    "readme.md", "readme.rst", "readme.txt",
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "cargo.toml",
    "go.mod", "pom.xml", "build.gradle", "gemfile",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "makefile", "justfile",
    ".github", "direction.md", "contributing.md", "changelog.md",
}


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
        headers = {
            "Authorization": f"Bearer {settings.github_pat}",
            "Accept": "application/vnd.github+json",
        }
        r = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/issues?state=open&per_page=30",
            headers=headers,
            follow_redirects=True,
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
    return json.dumps(
        {"ok": True, "owner": owner, "repo": repo, "DIRECTION_md": text}, default=str
    )[:14000]


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
    return json.dumps({"ok": True, "path": path, "ref": branch, "content": text}, default=str)[
        :14000
    ]


async def _github_repo_structure(owner_repo: str) -> str:
    """
    Fetch repo file tree + metadata from GitHub (no file content read).
    Returns language, top-level dirs, notable config/doc files, file count, and depth.
    Use as fallback when DIRECTION.md and README are absent.
    """
    if not settings.github_pat:
        return json.dumps({"ok": False, "message": "GITHUB_PAT not configured"})
    parsed = parse_owner_repo(owner_repo)
    if not parsed:
        return json.dumps({"ok": False, "message": "owner_repo must be owner/name"})
    owner, repo = parsed
    async with httpx.AsyncClient(timeout=30.0) as client:
        meta = await fetch_repo_metadata(client, owner, repo)
    if not meta:
        return json.dumps({"ok": False, "message": f"Could not fetch metadata for {owner}/{repo}"})

    notable: List[str] = []
    file_count = 0
    for sig in meta.raw_signals:
        if sig.startswith("file:"):
            file_count += 1
            basename = sig[5:]
            if basename in _NOTABLE_FILE_BASENAMES:
                notable.append(basename)
        elif sig.startswith("dir:"):
            pass

    return json.dumps({
        "ok": True,
        "repo": meta.full_name,
        "language": meta.language,
        "default_branch": meta.default_branch,
        "size_kb": meta.size_kb,
        "top_level_dirs": meta.top_level_dirs,
        "notable_files": notable,
        "total_unique_files": file_count,
        "max_depth": meta.max_depth,
    }, default=str)


async def _github_repo_planning_context(owner_repo: str, commits_limit: int = 20) -> str:
    """
    One call: DIRECTION.md + recent commits + open issues (same signals as server scan).
    Use before proposing Plaky tasks for a GitHub repo without a local clone.
    Falls back to README.md automatically when DIRECTION.md is absent.
    """
    if not settings.github_pat:
        return json.dumps({"ok": False, "message": "GITHUB_PAT not configured"})
    parsed = parse_owner_repo(owner_repo)
    if not parsed:
        return json.dumps({"ok": False, "message": "owner_repo must be owner/name"})
    owner, repo = parsed
    lim = max(5, min(int(commits_limit) if commits_limit else 20, 50))
    async with httpx.AsyncClient(timeout=90.0) as client:
        direction = await fetch_direction_md(client, owner, repo)
        commits = await fetch_recent_commits(client, owner, repo, limit=lim)
        issues = await fetch_open_issues(client, owner, repo)
        readme: Optional[str] = None
        if direction.startswith("(No DIRECTION.md"):
            raw = await fetch_repo_file_text(client, owner, repo, "README.md")
            if not raw.startswith("(file unavailable"):
                readme = raw
    out = {
        "ok": True,
        "repo": f"{owner}/{repo}",
        "DIRECTION_md": direction,
        "readme_md": readme,
        "recent_commits_markdown": commits,
        "open_issues_markdown": issues,
    }
    return json.dumps(out, default=str)[:24000]


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
            "Bundle DIRECTION.md + recent commits + open issues for owner/repo in one call — "
            "best starting point when planning work for a remote GitHub repo. "
            "Automatically falls back to README.md (returned as readme_md) when DIRECTION.md is absent. "
            "Optional commits_limit (default 20, max 50). Requires GITHUB_PAT."
        ),
    )


def build_github_tools() -> list[StructuredTool]:
def github_repo_structure_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_github_repo_structure,
        name="github_repo_structure",
        description=(
            "Fetch repo file tree and metadata from GitHub without reading file contents. "
            "Returns: primary language, default branch, top-level directories, notable files "
            "(README, Dockerfile, package.json, pyproject.toml, etc.), file count, and directory depth. "
            "Use as a fallback when DIRECTION.md and README are absent to infer repo purpose from structure."
        ),
    )


def build_github_tools() -> List[StructuredTool]:
    return [
        github_list_workspace_repos_tool(),
        github_repo_planning_context_tool(),
        github_repo_structure_tool(),
        github_fetch_direction_tool(),
        github_fetch_file_tool(),
        github_list_open_issues_tool(),
    ]
