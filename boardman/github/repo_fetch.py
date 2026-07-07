"""GitHub REST helpers for repo content (shared by scan + agent tools)."""

from __future__ import annotations

import base64

import httpx

from boardman.settings import settings


async def github_request(client: httpx.AsyncClient, path: str) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {settings.github_pat}",
        "Accept": "application/vnd.github+json",
    }
    return await client.get(f"https://api.github.com{path}", headers=headers)


def github_request_sync(client: httpx.Client, path: str) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {settings.github_pat}",
        "Accept": "application/vnd.github+json",
    }
    return client.get(f"https://api.github.com{path}", headers=headers)
    headers = {"Authorization": f"Bearer {settings.github_pat}", "Accept": "application/vnd.github+json"}
    # follow_redirects: renamed repos return 301 to the new owner/name; without this every
    # helper sees the bare 301 and reports the repo as inaccessible.
    return await client.get(f"https://api.github.com{path}", headers=headers, follow_redirects=True)


def github_request_sync(client: httpx.Client, path: str) -> httpx.Response:
    headers = {"Authorization": f"Bearer {settings.github_pat}", "Accept": "application/vnd.github+json"}
    return client.get(f"https://api.github.com{path}", headers=headers, follow_redirects=True)


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
    r = await github_request(client, f"/repos/{owner}/{repo}/contents/DIRECTION.md?ref=main")
    if r.status_code == 404:
        r = await github_request(client, f"/repos/{owner}/{repo}/contents/DIRECTION.md?ref=master")
    if r.status_code != 200:
        return f"(No DIRECTION.md found or inaccessible: HTTP {r.status_code})"
    data = r.json()
    if isinstance(data, dict) and data.get("encoding") == "base64" and data.get("content"):
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    if isinstance(data, dict) and data.get("message"):
        return f"(GitHub: {data.get('message')})"
    return "(Could not decode DIRECTION.md)"


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
    async with httpx.AsyncClient(timeout=20) as client:
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
) -> str:
    """Fetch a single file from the repo (default branch if ref empty)."""
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
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")[:50000]
    if isinstance(data, list):
        return f"(path {path} is a directory, not a file)"
    if isinstance(data, dict) and data.get("message"):
        return f"(GitHub: {data.get('message')})"
    return "(Could not decode file)"


async def fetch_default_branch(client: httpx.AsyncClient, owner: str, repo: str) -> str:
    r = await github_request(client, f"/repos/{owner}/{repo}")
    if r.status_code != 200:
        return "main"
    data = r.json()
    if isinstance(data, dict):
        b = data.get("default_branch")
        if isinstance(b, str) and b.strip():
            return b.strip()
    return "main"


def parse_owner_repo(owner_repo: str) -> tuple[str, str] | None:
    return _parse_owner_repo(owner_repo)
