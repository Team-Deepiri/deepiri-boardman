"""Which repos in the org actually get worked on.

Asked to rank the org by issue and PR volume, Boardman had to say it could not: no tool
exposed anything in bulk, only per-repo listings once you had already picked a repo. It
answered honestly, which is right, but "I cannot" is not the answer to a question the team
needs before deciding which repos to watch.

The ranking is nearly free. The org crawl the assistant already runs on most turns
downloads ``open_issues_count`` and ``pushed_at`` for every repository and used to throw
both away. Ranking reuses that. The only extra cost is splitting issues from pull requests
for the handful of repos at the top, one call each, because GitHub's ``open_issues_count``
counts pull requests as issues and reporting it as an issue count would be wrong.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Splitting issues from PRs costs one call per repo, so only the head of the list pays it.
_SPLIT_TOP_N = 8


async def _open_pr_count(client: Any, full_name: str, headers: dict[str, str]) -> int | None:
    """Open PRs for one repo, or None when it could not be read.

    None and 0 mean different things and must never be conflated: one is "no pull
    requests", the other is "I do not know".
    """
    owner, _, name = full_name.partition("/")
    if not name:
        return None
    try:
        r = await client.get(
            f"https://api.github.com/repos/{owner}/{name}/pulls?state=open&per_page=100",
            headers=headers,
        )
    except Exception:
        return None
    if r.status_code != 200:
        return None
    try:
        rows = r.json()
    except ValueError:
        return None
    return len(rows) if isinstance(rows, list) else None


async def org_activity_ranking(*, limit: int = 8, split_top: int = _SPLIT_TOP_N) -> dict[str, Any]:
    """Repos ranked by open work, most active first.

    Returns rows with ``open_issues_and_prs`` for every repo, and for the busiest few, a
    real ``open_prs`` / ``open_issues`` split. Rows the split could not be read for say so
    rather than guessing.
    """
    from boardman.github.http import github_http_client
    from boardman.github.org_repos import (
        cached_org_repo_rows,
        fetch_org_repository_full_names,
    )
    from boardman.settings import settings

    org = (settings.github_org or "").strip()
    token = (settings.github_pat or "").strip()
    if not org or not token:
        return {"ok": False, "message": "GITHUB_ORG and GITHUB_PAT are required"}

    client = github_http_client()
    rows = cached_org_repo_rows(org, skip_archived=settings.github_skip_archived)
    if not rows:
        # Warms both caches; the names call is the one the rest of the agent already uses.
        await fetch_org_repository_full_names(
            client, org, skip_archived=settings.github_skip_archived
        )
        rows = cached_org_repo_rows(org, skip_archived=settings.github_skip_archived)
    if not rows:
        return {"ok": False, "message": f"could not list repositories for {org}"}

    ranked = sorted(
        rows,
        key=lambda r: (int(r.get("open_issues_and_prs") or 0), str(r.get("pushed_at") or "")),
        reverse=True,
    )
    head = ranked[: max(0, min(int(split_top), len(ranked)))]

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    sem = asyncio.Semaphore(4)

    async def split(row: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            prs = await _open_pr_count(client, str(row.get("full_name") or ""), headers)
        out = dict(row)
        if prs is None:
            out["open_prs"] = None
            out["open_issues"] = None
            out["note"] = "PR count unavailable; open_issues_and_prs includes pull requests"
        else:
            total = int(row.get("open_issues_and_prs") or 0)
            out["open_prs"] = prs
            out["open_issues"] = max(0, total - prs)
        return out

    detailed = list(await asyncio.gather(*(split(r) for r in head)))
    tail = ranked[len(head) : max(len(head), limit)]

    return {
        "ok": True,
        "org": org,
        "repos_seen": len(rows),
        "ranked": (detailed + tail)[: max(1, limit)],
        "counting_note": (
            "open_issues_and_prs is GitHub's open_issues_count, which INCLUDES pull "
            "requests. open_issues/open_prs are split only for the busiest repos; a null "
            "means the split could not be read, not zero."
        ),
    }


def format_activity_markdown(payload: dict[str, Any]) -> str:
    """The ranking as a compact table a person can act on."""
    if not payload.get("ok"):
        return f"Could not rank org activity: {payload.get('message') or 'unknown error'}"
    lines = [f"Busiest repos in `{payload.get('org')}` ({payload.get('repos_seen')} seen):", ""]
    for i, row in enumerate(payload.get("ranked") or [], start=1):
        name = row.get("full_name")
        if row.get("open_prs") is None and "open_prs" in row:
            detail = f"{row.get('open_issues_and_prs')} open items (issue/PR split unavailable)"
        elif "open_prs" in row:
            detail = f"{row.get('open_issues')} open issues, {row.get('open_prs')} open PRs"
        else:
            detail = f"{row.get('open_issues_and_prs')} open items (issues + PRs)"
        pushed = str(row.get("pushed_at") or "")[:10]
        lines.append(f"{i}. `{name}` — {detail}" + (f", last push {pushed}" if pushed else ""))
    return "\n".join(lines)
