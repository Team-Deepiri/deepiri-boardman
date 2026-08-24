"""GitHub REST helpers for repo content (shared by scan + agent tools)."""

from __future__ import annotations

import base64
import logging
import time

import httpx

from boardman.github.http import shared_github_client
from boardman.settings import settings

_log = logging.getLogger(__name__)


async def github_request(client: httpx.AsyncClient, path: str) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {settings.github_pat}",
        "Accept": "application/vnd.github+json",
    }
    # follow_redirects: renamed repos return 301 to the new owner/name; without this every
    # helper sees the bare 301 and reports the repo as inaccessible.
    started = time.monotonic()
    try:
        return await client.get(
            f"https://api.github.com{path}", headers=headers, follow_redirects=True
        )
    finally:
        _log.debug("GitHub GET %s took %.2fs", path, time.monotonic() - started)


def github_request_sync(client: httpx.Client, path: str) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {settings.github_pat}",
        "Accept": "application/vnd.github+json",
    }
    started = time.monotonic()
    try:
        return client.get(f"https://api.github.com{path}", headers=headers, follow_redirects=True)
    finally:
        _log.debug("GitHub GET %s took %.2fs", path, time.monotonic() - started)


def _parse_owner_repo(owner_repo: str) -> tuple[str, str] | None:
    s = (owner_repo or "").strip()
    if "/" not in s:
        return None
    owner, repo = s.split("/", 1)
    owner, repo = owner.strip(), repo.strip()
    if not owner or not repo:
        return None
    return owner, repo


async def fetch_direction_md(client: httpx.AsyncClient, owner: str, repo: str) -> str:
    # Default branch first (repos whose default is not main/master), then the legacy pair.
    branches: list[str] = []
    default = await fetch_default_branch(client, owner, repo)
    for b in (default, "main", "master"):
        if b and b not in branches:
            branches.append(b)
    r = None
    for b in branches:
        r = await github_request(client, f"/repos/{owner}/{repo}/contents/DIRECTION.md?ref={b}")
        if r.status_code == 200:
            break
    if r is None or r.status_code != 200:
        code = r.status_code if r is not None else "n/a"
        return f"(No DIRECTION.md found or inaccessible: HTTP {code})"
    data = r.json()
    if isinstance(data, dict) and data.get("encoding") == "base64" and data.get("content"):
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    if isinstance(data, dict) and data.get("message"):
        return f"(GitHub: {data.get('message')})"
    return "(Could not decode DIRECTION.md)"


async def fetch_readme_md(client: httpx.AsyncClient, owner: str, repo: str) -> str:
    """Repo README via GET /repos/{o}/{r}/readme (any filename, default branch)."""
    r = await github_request(client, f"/repos/{owner}/{repo}/readme")
    if r.status_code != 200:
        return f"(No README found or inaccessible: HTTP {r.status_code})"
    data = r.json()
    if isinstance(data, dict) and data.get("encoding") == "base64" and data.get("content"):
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")[:30000]
    if isinstance(data, dict) and data.get("message"):
        return f"(GitHub: {data.get('message')})"
    return "(Could not decode README)"


# Manifest/config files worth surfacing when a repo has no docs.
MANIFEST_FILENAMES = (
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "go.mod",
    "cargo.toml",
    "pom.xml",
    "build.gradle",
    "gemfile",
    "composer.json",
    "docker-compose.yml",
    "docker-compose.yaml",
    "dockerfile",
    "makefile",
)


async def fetch_repo_overview(client: httpx.AsyncClient, owner: str, repo: str) -> dict:
    """Doc-free structural context: metadata + languages + file-tree summary + manifest paths.

    This is the fallback that lets the agent explain a repo with NO markdown files:
    what it is (description/topics), what it is written in (languages), how it is
    laid out (tree summary), and where to look next (manifests + entry points).
    """
    from boardman.github.repo_metadata import fetch_repo_identity, fetch_repo_tree

    out: dict = {"full_name": f"{owner}/{repo}"}

    data = await fetch_repo_identity(client, owner, repo)
    if data:
        out["description"] = data.get("description") or ""
        out["topics"] = data.get("topics") or []
        out["default_branch"] = data.get("default_branch") or "main"
        out["pushed_at"] = data.get("pushed_at") or ""
        out["archived"] = bool(data.get("archived"))
    else:
        out["error"] = "repo metadata unavailable"
        return out

    rl = await github_request(client, f"/repos/{owner}/{repo}/languages")
    langs = rl.json() if rl.status_code == 200 else None
    if isinstance(langs, dict):
        total = sum(v for v in langs.values() if isinstance(v, int | float)) or 1
        out["languages"] = {
            k: round(100.0 * v / total, 1)
            for k, v in sorted(langs.items(), key=lambda kv: -kv[1])[:8]
        }

    branch = out.get("default_branch", "main")
    tree_payload = await fetch_repo_tree(client, owner, repo, branch)
    if tree_payload:
        tree = tree_payload.get("tree") or []
        paths = [t.get("path", "") for t in tree if isinstance(t, dict) and t.get("type") == "blob"]
        out["file_count"] = len(paths)
        out["truncated_tree"] = bool(tree_payload.get("truncated"))

        top_level: dict[str, int] = {}
        manifests: list[str] = []
        entry_points: list[str] = []
        for p in paths:
            head = p.split("/", 1)[0]
            top_level[head] = top_level.get(head, 0) + 1
            base = p.rsplit("/", 1)[-1].lower()
            if base in MANIFEST_FILENAMES and len(manifests) < 15:
                manifests.append(p)
            if (
                base
                in ("main.py", "app.py", "index.ts", "index.js", "main.go", "main.rs", "server.py")
                and len(entry_points) < 10
            ):
                entry_points.append(p)
        out["top_level"] = dict(sorted(top_level.items(), key=lambda kv: -kv[1])[:25])
        out["manifests"] = manifests
        out["entry_points"] = entry_points
        # A shallow path sample so the model sees real structure, not just counts.
        shallow = [p for p in paths if p.count("/") <= 1]
        out["path_sample"] = shallow[:80]
    else:
        out["tree_error"] = "tree unavailable"

    return out


async def fetch_recent_commits(
    client: httpx.AsyncClient, owner: str, repo: str, limit: int = 20
) -> str:
    r = await github_request(client, f"/repos/{owner}/{repo}/commits?per_page={limit}")
    if r.status_code != 200:
        return f"(commits unavailable: {r.status_code})"
    commits = r.json()
    if not isinstance(commits, list):
        return "(commits: unexpected response)"
    lines: list[str] = []
    for c in commits[:limit]:
        sha = (c.get("sha") or "")[:7]
        msg = (c.get("commit") or {}).get("message", "").split("\n")[0]
        lines.append(f"- {sha} {msg}")
    return "\n".join(lines) if lines else "(no commits)"


async def fetch_open_issues(client: httpx.AsyncClient, owner: str, repo: str) -> str:
    r = await github_request(client, f"/repos/{owner}/{repo}/issues?state=open&per_page=50")
    if r.status_code != 200:
        return f"(issues unavailable: {r.status_code})"
    issues = r.json()
    if not isinstance(issues, list):
        return "(issues: unexpected response)"
    lines: list[str] = []
    for i in issues:
        if "pull_request" in i:
            continue
        lines.append(f"- #{i['number']}: {i.get('title', '')}")
    return "\n".join(lines) if lines else "(no open issues)"


async def fetch_open_pull_requests(client: httpx.AsyncClient, owner: str, repo: str) -> str:
    """Open PRs with draft/author. Work in flight is stronger PM signal than a filed issue —
    a repo with 8 open PRs and 0 issues is busy, not idle, and the assistant must see that."""
    r = await github_request(client, f"/repos/{owner}/{repo}/pulls?state=open&per_page=50")
    if r.status_code != 200:
        return f"(pull requests unavailable: {r.status_code})"
    prs = r.json()
    if not isinstance(prs, list):
        return "(pull requests: unexpected response)"
    lines: list[str] = []
    for p in prs:
        if not isinstance(p, dict):
            continue
        author = ((p.get("user") or {}) if isinstance(p.get("user"), dict) else {}).get("login", "")
        draft = " [draft]" if p.get("draft") else ""
        lines.append(f"- #{p.get('number')}: {p.get('title', '')} (by {author}){draft}")
    return chr(10).join(lines) if lines else "(no open pull requests)"


async def fetch_pr_assignees_and_reviewers_logins(full_name: str, pr_number: int) -> set[str]:
    """
    GitHub assignees + requested_reviewers for a PR (lowercased logins).
    Used when `issue_comment` payloads omit full PR metadata.
    """
    parsed = _parse_owner_repo(full_name)
    if not parsed or not (settings.github_pat or "").strip():
        return set()
    owner, repo = parsed
    from urllib.parse import quote

    path = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{int(pr_number)}"
    async with shared_github_client() as client:
        r = await github_request(client, path)
    if r.status_code != 200:
        return set()
    data = r.json()
    if not isinstance(data, dict):
        return set()
    out: set[str] = set()
    for key in ("assignees", "requested_reviewers"):
        block = data.get(key)
        if isinstance(block, list):
            for u in block:
                if isinstance(u, dict) and isinstance(u.get("login"), str):
                    out.add(u["login"].strip().casefold())
    return out


async def fetch_repo_file_text(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    path: str,
    *,
    ref: str = "",
    max_chars: int = 50_000,
) -> str:
    """Fetch a single file from the repo (default branch if ref empty).

    ``max_chars`` caps the returned text. The default keeps prompt payloads small; callers
    that COUNT things in the file (defect scanning) must raise it, otherwise they silently
    report an undercount for any file larger than the cap.
    """
    from boardman.github.read_cache import cached

    async def _fetch_full() -> str:
        """Fetch and decode WITHOUT truncating — the memo must store the full text.

        Callers pass different max_chars (defect scan 120k, prompt reads 50k). Caching an
        already-truncated string would serve 50k-cut text to the 120k counter and
        silently undercount defects, so the cap is applied per caller AFTER the memo.
        """
        from urllib.parse import quote

        clean = (path or "").strip().lstrip("/")
        enc = "/".join(quote(seg, safe="") for seg in clean.split("/") if seg)
        q = f"/repos/{owner}/{repo}/contents/{enc}"
        if ref.strip():
            q += f"?ref={quote(ref.strip(), safe='')}"
        r = await github_request(client, q)
        if r.status_code != 200:
            return f"(file unavailable: HTTP {r.status_code} for {path})"
        data = r.json()
        if isinstance(data, dict) and data.get("encoding") == "base64" and data.get("content"):
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        if isinstance(data, list):
            return f"(path {path} is a directory, not a file)"
        if isinstance(data, dict) and data.get("message"):
            return f"(GitHub: {data.get('message')})"
        return "(Could not decode file)"

    # Scan + search fetch the SAME top-N files back to back; without this every audit
    # question re-downloaded ~16 files (up to ~2.5MB) per tool call. Error strings all
    # start with "(" and are never cached, so a 403 blip is retried, not remembered.
    full = await cached(
        f"file:{owner}/{repo}@{ref.strip()}:{path}",
        _fetch_full,
        ok=lambda s: isinstance(s, str) and not s.startswith("("),
    )
    text = str(full)
    if len(text) > max_chars:
        _log.warning(
            "fetch_repo_file_text truncated %s/%s:%s from %d to %d chars",
            owner, repo, path, len(text), max_chars,
        )
    return text[:max_chars]


async def fetch_default_branch(client: httpx.AsyncClient, owner: str, repo: str) -> str:
    from boardman.github.repo_metadata import fetch_repo_identity

    data = await fetch_repo_identity(client, owner, repo)
    if data:
        b = data.get("default_branch")
        if isinstance(b, str) and b.strip():
            return b.strip()
    return "main"


def parse_owner_repo(owner_repo: str) -> tuple[str, str] | None:
    return _parse_owner_repo(owner_repo)
