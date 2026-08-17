"""GitHub read-only helpers for the agent."""

from __future__ import annotations

import json
from typing import Any

import httpx
from langchain_core.tools import StructuredTool

from boardman.github.code_search import scan_repo_defects, search_repo_code
from boardman.github.http import shared_github_client
from boardman.github.read_cache import cached, json_ok
from boardman.github.repo_fetch import (
    fetch_default_branch,
    fetch_direction_md,
    fetch_open_issues,
    fetch_open_pull_requests,
    fetch_recent_commits,
    fetch_repo_file_text,
    parse_owner_repo,
)
from boardman.github.repo_hotspots import fetch_repo_hotspots
from boardman.github.repo_metadata import fetch_repo_metadata
from boardman.repos_config import list_workspace_repos
from boardman.settings import settings

_NOTABLE_FILE_BASENAMES = {
    "readme.md",
    "readme.rst",
    "readme.txt",
    "package.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "gemfile",
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "makefile",
    "justfile",
    ".github",
    "direction.md",
    "contributing.md",
    "changelog.md",
}


async def _workspace_repo_suggestions(
    client: httpx.AsyncClient, requested: str, limit: int = 5
) -> list[str]:
    """Closest workspace repos to a requested name (users say 'deepiri-cyrex' for 'diri-cyrex')."""
    from difflib import SequenceMatcher

    try:
        repos = await list_workspace_repos(client)
        names = list(repos.keys())
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


def _repo_not_found_payload(owner: str, repo: str, suggestions: list[str]) -> str:
    return json.dumps(
        {
            "ok": False,
            "repo": f"{owner}/{repo}",
            "repo_not_found": True,
            "message": (
                f"GitHub repo {owner}/{repo} does not exist or is inaccessible. "
                "Do NOT invent an analysis for it."
            ),
            "did_you_mean": suggestions,
            "guidance": (
                "If one of did_you_mean matches the user's intent, call this tool again with that "
                "exact owner/repo. Otherwise ask the user to confirm the name (you can also call "
                "github_list_workspace_repos)."
            ),
        }
    )


async def _github_list_workspace_repos() -> str:
    """List all GitHub repositories in the configured org merged with repos.yml config."""
    if not settings.github_pat:
        return json.dumps({"ok": False, "message": "GITHUB_PAT not configured"})
    async with shared_github_client() as client:
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


async def _github_read_pull_request(owner_repo: str, pr_number: int) -> str:
    """Open one PR: description, changed files, review verdicts, CI. Text, not JSON —
    a merge judgement reads better from the rendered summary than from nested objects."""
    from boardman.github.pr_review_context import (
        fetch_pull_request_context,
        render_pull_request_context,
    )

    try:
        number = int(pr_number)
    except (TypeError, ValueError):
        return "pr_number must be an integer (the number after '#')."
    ctx = await fetch_pull_request_context(owner_repo, number)
    return render_pull_request_context(ctx)


async def _github_list_pull_requests(owner_repo: str, state: str = "open") -> str:
    if not settings.github_pat:
        return json.dumps({"ok": False, "message": "GITHUB_PAT not configured"})
    parsed = parse_owner_repo(owner_repo)
    if not parsed:
        return json.dumps({"ok": False, "message": "owner_repo must be owner/name"})
    owner, repo = parsed
    want = (state or "open").strip().lower()
    if want not in ("open", "closed", "all"):
        want = "open"
    async with shared_github_client() as client:
        r = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls?state={want}&per_page=30",
            headers={
                "Authorization": f"Bearer {settings.github_pat}",
                "Accept": "application/vnd.github+json",
            },
            follow_redirects=True,
        )
    if r.status_code != 200:
        return json.dumps({"ok": False, "status": r.status_code, "text": r.text[:300]})
    prs = r.json()
    if not isinstance(prs, list):
        return json.dumps({"ok": False, "message": "unexpected response"})
    slim = [
        {
            "number": p.get("number"),
            "title": p.get("title"),
            "author": (
                (p.get("user") or {}).get("login") if isinstance(p.get("user"), dict) else ""
            ),
            "draft": bool(p.get("draft")),
            "state": p.get("state"),
            "url": p.get("html_url"),
        }
        for p in prs
        if isinstance(p, dict)
    ]
    return json.dumps({"ok": True, "state": want, "returned": len(slim), "pull_requests": slim})


async def _github_list_open_issues(owner_repo: str) -> str:
    if not settings.github_pat:
        return json.dumps({"ok": False, "message": "GITHUB_PAT not configured"})
    parsed = parse_owner_repo(owner_repo)
    if not parsed:
        return json.dumps({"ok": False, "message": "owner_repo must be owner/name"})
    owner, repo = parsed
    async with shared_github_client() as client:
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
    async with shared_github_client() as client:
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
    async with shared_github_client() as client:
        branch = (ref or "").strip()
        if not branch:
            branch = await fetch_default_branch(client, owner, repo)
        text = await fetch_repo_file_text(client, owner, repo, path.strip(), ref=branch)
    return json.dumps({"ok": True, "path": path, "ref": branch, "content": text}, default=str)[
        :14000
    ]


async def _github_repo_structure(owner_repo: str) -> str:
    """Repo shape (tree + metadata). Cached: the file tree does not change mid-conversation."""
    return await cached(
        f"structure:{(owner_repo or '').strip().lower()}",
        lambda: _github_repo_structure_uncached(owner_repo),
        ok=json_ok,
    )


async def _github_repo_structure_uncached(owner_repo: str) -> str:
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
    async with shared_github_client() as client:
        meta = await fetch_repo_metadata(client, owner, repo)
        if not meta:
            suggestions = await _workspace_repo_suggestions(client, repo)
            return _repo_not_found_payload(owner, repo, suggestions)

    notable: list[str] = []
    file_count = 0
    for sig in meta.raw_signals:
        if sig.startswith("file:"):
            file_count += 1
            basename = sig[5:]
            if basename in _NOTABLE_FILE_BASENAMES:
                notable.append(basename)
        elif sig.startswith("dir:"):
            pass

    return json.dumps(
        {
            "ok": True,
            "repo": meta.full_name,
            "language": meta.language,
            "default_branch": meta.default_branch,
            "size_kb": meta.size_kb,
            "top_level_dirs": meta.top_level_dirs,
            "notable_files": notable,
            "total_unique_files": file_count,
            "max_depth": meta.max_depth,
        },
        default=str,
    )


def _context_budget() -> int:
    return int(getattr(settings, "llm_context_budget_chars", 0) or 24000)


# Longest first: a repo's own docs earn more room than the commit list.
_TRIMMABLE = (
    ("DIRECTION_md", 8000),
    ("readme_md", 8000),
    ("open_pull_requests_markdown", 3000),
    ("open_issues_markdown", 3000),
    ("recent_commits_markdown", 3000),
)


def _budget_json(out: dict[str, Any]) -> str:
    """Serialize within budget by trimming FIELDS, never the serialized JSON.

    Slicing `json.dumps(...)[:24000]` cuts mid-string: the payload stops being valid JSON
    and whatever came after it disappears with no trace, so a partial read looks like a
    complete one. Trim the long text fields instead, mark each cut inline, and keep the
    envelope parseable.
    """
    trimmed: list[str] = []
    for key, cap in _TRIMMABLE:
        text = out.get(key)
        if isinstance(text, str) and len(text) > cap:
            out[key] = text[:cap] + f"\n\n…[truncated: {len(text) - cap} more characters]"
            trimmed.append(key)

    payload = json.dumps(out, default=str)
    if len(payload) > _context_budget():
        # Still over: drop the least load-bearing sections outright rather than corrupt
        # the JSON, and say which ones went.
        for key, _ in reversed(_TRIMMABLE):
            if key not in out:
                continue
            out[key] = "[omitted to fit the context budget — fetch it directly if needed]"
            trimmed.append(key)
            payload = json.dumps(out, default=str)
            if len(payload) <= _context_budget():
                break

    if trimmed:
        out["truncated_fields"] = sorted(set(trimmed))
        payload = json.dumps(out, default=str)
    return payload


async def _github_repo_planning_context(owner_repo: str, commits_limit: int = 20) -> str:
    """
    One call: DIRECTION.md + recent commits + open issues (same signals as server scan).
    Use before proposing Plaky tasks for a GitHub repo without a local clone.
    Falls back to README.md automatically when DIRECTION.md is absent.

    Cached per repo for a few minutes: follow-up questions about the same repo are the
    common case, and re-fetching seven endpoints to answer "and what about its tests?"
    is latency the user pays for nothing.
    """
    return await cached(
        f"planning:{(owner_repo or '').strip().lower()}:{commits_limit}",
        lambda: _github_repo_planning_context_uncached(owner_repo, commits_limit),
        ok=json_ok,
    )


async def _github_repo_planning_context_uncached(owner_repo: str, commits_limit: int = 20) -> str:
    if not settings.github_pat:
        return json.dumps({"ok": False, "message": "GITHUB_PAT not configured"})
    raw_name = (owner_repo or "").strip()
    parsed = parse_owner_repo(raw_name)
    if not parsed and raw_name and "/" not in raw_name:
        # Bare name: assume the configured default owner instead of erroring out.
        from boardman.assignment.qa_picker import ensure_github_owner_repo

        parsed = parse_owner_repo(ensure_github_owner_repo(raw_name))
    if not parsed:
        return json.dumps({"ok": False, "message": "owner_repo must be owner/name"})
    owner, repo = parsed
    lim = max(5, min(int(commits_limit) if commits_limit else 20, 50))
    import asyncio

    async with shared_github_client() as client:
        # Every signal in ONE round trip instead of five sequential ones — this tool is the
        # hot path for "analyze this repo" questions, where serial fetches dominated latency.
        # README is fetched unconditionally (it is the fallback when DIRECTION.md is absent,
        # which is the common case) rather than costing an extra sequential hop.
        meta, direction, commits, issues, readme_raw, hotspots, open_prs = await asyncio.gather(
            fetch_repo_metadata(client, owner, repo),
            fetch_direction_md(client, owner, repo),
            fetch_recent_commits(client, owner, repo, limit=lim),
            fetch_open_issues(client, owner, repo),
            fetch_repo_file_text(client, owner, repo, "README.md"),
            fetch_repo_hotspots(client, owner, repo),
            fetch_open_pull_requests(client, owner, repo),
            return_exceptions=True,
        )

        def _text(v: Any, missing: str) -> str:
            return v if isinstance(v, str) else missing

        meta = meta if not isinstance(meta, BaseException) else None
        if meta is None:
            # Wrong/misspelled repo: return did_you_mean rather than "(No DIRECTION.md ...)"
            # strings, which the model otherwise reads as "the repo is empty".
            suggestions = await _workspace_repo_suggestions(client, repo)
            return _repo_not_found_payload(owner, repo, suggestions)
        direction = _text(direction, "(DIRECTION.md unavailable)")
        commits = _text(commits, "(commits unavailable)")
        issues = _text(issues, "(issues unavailable)")
        readme_text = _text(readme_raw, "")
        readme: str | None = (
            readme_text if readme_text and not readme_text.startswith("(file unavailable") else None
        )
        code_signals = hotspots if isinstance(hotspots, dict) else None
        prs_md = _text(open_prs, "(pull requests unavailable)")

    # Structural summary inline so the model does not need a second github_repo_structure
    # call just to know what the repo is made of.
    notable = sorted(
        {
            sig[5:]
            for sig in getattr(meta, "raw_signals", []) or []
            if sig.startswith("file:") and sig[5:] in _NOTABLE_FILE_BASENAMES
        }
    )
    out = {
        "ok": True,
        "repo": f"{owner}/{repo}",
        "structure": {
            "language": getattr(meta, "language", ""),
            "default_branch": getattr(meta, "default_branch", ""),
            "size_kb": getattr(meta, "size_kb", 0),
            "top_level_dirs": getattr(meta, "top_level_dirs", []),
            "notable_files": notable,
            "max_depth": getattr(meta, "max_depth", 0),
        },
        # Source-level evidence: largest files, test ratio, and committed-artifact smells.
        # Doc-and-issue reading alone cannot answer "what's actually wrong with this repo".
        "code_signals": code_signals,
        "DIRECTION_md": direction,
        "readme_md": readme,
        "recent_commits_markdown": commits,
        "open_issues_markdown": issues,
        "open_pull_requests_markdown": prs_md,
    }
    return _budget_json(out)


async def _github_search_code(owner_repo: str, query: str) -> str:
    """Grep a GitHub repo for a literal string / symbol and return matching lines."""
    if not settings.github_pat:
        return json.dumps({"ok": False, "message": "GITHUB_PAT not configured"})
    raw = (owner_repo or "").strip()
    parsed = parse_owner_repo(raw)
    if not parsed and raw and "/" not in raw:
        from boardman.assignment.qa_picker import ensure_github_owner_repo

        parsed = parse_owner_repo(ensure_github_owner_repo(raw))
    if not parsed:
        return json.dumps({"ok": False, "message": "owner_repo must be owner/name"})
    owner, repo = parsed
    async with shared_github_client() as client:
        out = await search_repo_code(client, owner, repo, query)
    if out is None:
        return json.dumps({"ok": False, "message": "code search unavailable"})
    return json.dumps(out, default=str)[:12000]


async def _github_scan_defects(owner_repo: str) -> str:
    """Defect probes over the largest source files. Cached: it reads many files, and the
    same audit question is usually asked several ways in one conversation."""
    return await cached(
        f"defects:{(owner_repo or '').strip().lower()}",
        lambda: _github_scan_defects_uncached(owner_repo),
        ok=json_ok,
    )


async def _github_scan_defects_uncached(owner_repo: str) -> str:
    """Read the repo's largest source files and report real defect lines."""
    if not settings.github_pat:
        return json.dumps({"ok": False, "message": "GITHUB_PAT not configured"})
    raw = (owner_repo or "").strip()
    parsed = parse_owner_repo(raw)
    if not parsed and raw and "/" not in raw:
        from boardman.assignment.qa_picker import ensure_github_owner_repo

        parsed = parse_owner_repo(ensure_github_owner_repo(raw))
    if not parsed:
        return json.dumps({"ok": False, "message": "owner_repo must be owner/name"})
    owner, repo = parsed
    async with shared_github_client() as client:
        out = await scan_repo_defects(client, owner, repo)
    if out is None:
        return json.dumps({"ok": False, "message": "could not read repo source"})
    return json.dumps(out, default=str)[:14000]


def github_list_workspace_repos_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_github_list_workspace_repos,
        name="github_list_workspace_repos",
        description=(
            "List all GitHub repositories in the configured org merged with repos.yml. "
            "Use this when you need to know which repos are available to the agent."
        ),
    )


def github_read_pull_request_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_github_read_pull_request,
        name="github_read_pull_request",
        description=(
            "Open ONE pull request and read what a reviewer reads: description, changed files "
            "with per-file +/- counts, each reviewer's latest verdict, requested reviewers, CI "
            "check results, commit subjects, and the issues it closes. ALWAYS call this before "
            "answering anything about a specific PR — 'is #12 safe to merge', 'what does this PR "
            "change', 'who reviewed it', 'why is CI red'. Never judge a PR from its title alone. "
            "Sections that GitHub refused are labelled UNAVAILABLE: treat those as unknown, not "
            "as absent — 'reviews: UNAVAILABLE' does NOT mean nobody approved it. "
            "Args: owner_repo (e.g. Team-Deepiri/deepiri-boardman), pr_number."
        ),
    )


def github_list_pull_requests_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_github_list_pull_requests,
        name="github_list_pull_requests",
        description=(
            "List pull requests for owner/repo with number, title, author and draft flag. "
            "Args: owner_repo, optional state ('open' default, 'closed', or 'all'). "
            "Use github_read_pull_request for the contents of a specific one."
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


def github_search_code_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_github_search_code,
        name="github_search_code",
        description=(
            "Grep a GitHub repo for a literal string, symbol, or pattern and get the matching "
            "lines with file paths. Use this to turn a suspicion into evidence — e.g. search "
            "'except Exception', 'TODO', a function name, or a hardcoded id. "
            "Args: owner_repo, query."
        ),
    )


def github_scan_defects_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_github_scan_defects,
        name="github_scan_defects",
        description=(
            "Run standard defect probes over a repo (bare excepts, broad exception handlers, "
            "TODO/FIXME/HACK markers, stray prints) and return real matching lines with counts. "
            "Call this for 'find the problems / audit this repo' questions so findings cite "
            "actual code instead of file sizes. Args: owner_repo."
        ),
    )


def build_github_tools() -> list[StructuredTool]:
    return [
        github_list_workspace_repos_tool(),
        github_repo_planning_context_tool(),
        github_repo_structure_tool(),
        github_search_code_tool(),
        github_scan_defects_tool(),
        github_fetch_direction_tool(),
        github_fetch_file_tool(),
        github_list_open_issues_tool(),
        github_list_pull_requests_tool(),
        github_read_pull_request_tool(),
    ]
