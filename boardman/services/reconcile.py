"""Bounded GitHub to Plaky reconciliation. Webhooks are the fast path, this is the net.

A missed webhook (downtime, delivery failure, a bug fixed after the fact) leaves drift:
an open issue with no task, a task whose Type or assignee no longer matches GitHub, an
open PR with no link. This walks CURRENT GitHub state for a repo and replays it through
the same handlers the webhook path uses. Those handlers are already idempotent (the
live retro-sync of issues #82/#83 ran exactly this way), so reconciliation cannot
duplicate tasks or comments, and a second run over a healthy repo is a no-op.

Bounded on purpose: open items plus a single bounded page each, never full history.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from boardman.github.http import github_http_client
from boardman.github.webhooks import IssueEventPayload, PullRequestEventPayload
from boardman.services.issue_handler import (
    find_plaky_task_by_issue,
    handle_issue_labels_changed,
    handle_issue_opened,
)
from boardman.services.pr_handler import handle_pr_opened
from boardman.services.pr_task_registry import distinct_task_ids_for_pr
from boardman.settings import settings

logger = logging.getLogger(__name__)


def _gh_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.github_pat}",
        "Accept": "application/vnd.github+json",
    }


async def reconcile_repo(
    full_name: str,
    session: AsyncSession,
    *,
    max_items: int = 50,
) -> dict[str, Any]:
    """Detect and repair GitHub-Plaky drift for one repo. Safe to run repeatedly."""
    if not (settings.github_pat or "").strip():
        return {"ok": False, "message": "GITHUB_PAT is not configured"}
    owner, _, short = full_name.partition("/")
    if not owner or not short:
        return {"ok": False, "message": "repo must be owner/name"}
    repo_block = {"full_name": full_name, "name": short}
    client = github_http_client()

    out: dict[str, Any] = {
        "ok": True,
        "repo": full_name,
        "issues_checked": 0,
        "tasks_created": 0,
        "issues_resynced": 0,
        "prs_checked": 0,
        "prs_relinked": 0,
        "errors": [],
    }

    r = await client.get(
        f"https://api.github.com/repos/{full_name}/issues?state=open&per_page={max_items}",
        headers=_gh_headers(),
    )
    if r.status_code != 200:
        return {"ok": False, "message": f"GitHub issues list failed: HTTP {r.status_code}"}
    for issue in r.json():
        if not isinstance(issue, dict) or "pull_request" in issue:
            continue
        out["issues_checked"] += 1
        num = int(issue.get("number") or 0)
        try:
            mapping = await find_plaky_task_by_issue(short, num, session)
            if mapping is None or not mapping.plaky_task_id:
                res = await handle_issue_opened(
                    IssueEventPayload(action="opened", issue=issue, repository=repo_block),
                    session,
                )
                if res.get("plaky_task_id"):
                    out["tasks_created"] += 1
                    logger.info(
                        "reconcile: issue #%s had no task; created %s",
                        num,
                        res.get("plaky_task_id"),
                    )
            else:
                # Metadata sync is fill-only and idempotent: Type from native type or
                # labels, assignee filled when Plaky has none. Healthy items no-op.
                res = await handle_issue_labels_changed(
                    IssueEventPayload(action="labeled", issue=issue, repository=repo_block),
                    session,
                )
                if res.get("event") == "issue_labels_synced":
                    out["issues_resynced"] += 1
        except Exception as e:
            out["errors"].append(f"issue #{num}: {type(e).__name__}: {e}"[:200])

    r2 = await client.get(
        f"https://api.github.com/repos/{full_name}/pulls?state=open&per_page={max_items}",
        headers=_gh_headers(),
    )
    if r2.status_code != 200:
        out["errors"].append(f"GitHub pulls list failed: HTTP {r2.status_code}")
        return out
    for pr in r2.json():
        if not isinstance(pr, dict):
            continue
        out["prs_checked"] += 1
        num = int(pr.get("number") or 0)
        try:
            linked = await distinct_task_ids_for_pr(
                session, github_repo=short, github_pr_number=num
            )
            if linked:
                continue  # a stable link exists; PR events keep it current
            res = await handle_pr_opened(
                PullRequestEventPayload(action="opened", pull_request=pr, repository=repo_block),
                session,
            )
            if res.get("linked") or res.get("plaky_task_id"):
                out["prs_relinked"] += 1
                logger.info("reconcile: PR #%s was unlinked; repaired", num)
        except Exception as e:
            out["errors"].append(f"PR #{num}: {type(e).__name__}: {e}"[:200])

    out["ok"] = not out["errors"]
    return out
