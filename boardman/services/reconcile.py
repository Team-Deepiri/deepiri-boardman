"""Bounded GitHub to Plaky reconciliation. Webhooks are the fast path, this is the net.

A missed webhook (downtime, delivery failure, a bug fixed after the fact) leaves drift:
an open issue with no task, a task whose Type or assignee no longer matches GitHub, an
open PR with no link. This walks CURRENT GitHub state for a repo and replays it through
the same handlers the webhook path uses. Those handlers are already idempotent (the
live retro-sync of issues #82/#83 ran exactly this way), so reconciliation cannot
duplicate tasks or comments, and a second run over a healthy repo is a no-op.

Bounded on purpose: the latest bounded page of issues and PRs is fetched, including closed
items, so terminal-state drift can be repaired without walking full repository history.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from boardman.github.http import github_http_client
from boardman.github.webhooks import IssueEventPayload, PullRequestEventPayload
from boardman.observability.degradation import log_degraded
from boardman.services.issue_handler import (
    find_plaky_task_by_issue,
    handle_issue_opened,
)
from boardman.services.pr_handler import (
    handle_pr_closed_without_merge,
    handle_pr_edited,
    handle_pr_merged,
    handle_pr_opened,
)
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
        # Two different things, deliberately not one number. `prs_relinked` is REPAIR: the
        # PR had no task and now has one. `prs_resynced` is a PR that was already linked
        # and had its metadata and state replayed, which changes nothing when there is no
        # drift. Counting both as "relinked" made every run report ten repairs on a repo
        # where nothing was wrong, and a reconciliation you cannot read is one you cannot
        # verify.
        "prs_relinked": 0,
        "prs_resynced": 0,
        "errors": [],
    }

    r = await client.get(
        f"https://api.github.com/repos/{full_name}/issues?state=all&sort=updated&direction=desc&per_page={max_items}",
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
                if str(issue.get("state") or "open").casefold() != "open":
                    # Same rule as pull requests: a closed issue nobody ever tracked is
                    # history. handle_issue_opened would file it at NEEDS ASSIGNED, so
                    # the board would grow work items for things already finished.
                    out["issues_skipped_closed"] = out.get("issues_skipped_closed", 0) + 1
                    continue
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
                # The generalized issue metadata path also re-resolves priority,
                # title/body, assignee removal, and open/closed workflow state.
                if str(issue.get("state") or "open").casefold() == "closed":
                    from boardman.services.issue_handler import handle_issue_closed

                    res = await handle_issue_closed(
                        IssueEventPayload(action="closed", issue=issue, repository=repo_block),
                        session,
                    )
                else:
                    from boardman.services.issue_handler import handle_issue_edited

                    res = await handle_issue_edited(
                        IssueEventPayload(action="edited", issue=issue, repository=repo_block),
                        session,
                    )
                # `issue_labels_synced` came from a handler this path no longer calls, so
                # the counter read zero however much drift the sweep repaired -- and that
                # number is the whole operator-facing signal that it did anything.
                if res.get("ok") and not res.get("skipped"):
                    out["issues_resynced"] += 1
        except Exception as e:  # noqa: BLE001 - sync failure must not crash the service
            log_degraded(logger, f"reconcile_repo: reconciling issue #{num}", e)
            out["errors"].append(f"issue #{num}: {type(e).__name__}: {e}"[:200])

    r2 = await client.get(
        f"https://api.github.com/repos/{full_name}/pulls?state=all&sort=updated&direction=desc&per_page={max_items}",
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
                pr_state = str(pr.get("state") or "open").casefold()
                # GitHub's LIST pulls endpoint omits the `merged` boolean -- only the
                # single-PR GET carries it -- so reading it directly sent every merged PR
                # to the closed-without-merge handler. That withdraws links and rewinds a
                # task out of QA, every fifteen minutes, for PRs that shipped. `merged_at`
                # is on the list payload and says the same thing. Same derivation the
                # poller documents in `_pr_payload`.
                pr["merged"] = bool(pr.get("merged")) or bool(pr.get("merged_at"))
                if bool(pr.get("merged")):
                    res = await handle_pr_merged(
                        PullRequestEventPayload(
                            action="closed", pull_request=pr, repository=repo_block
                        ),
                        session,
                    )
                    out["prs_resynced"] += int(bool(res.get("updated")))
                elif pr_state == "closed":
                    res = await handle_pr_closed_without_merge(
                        PullRequestEventPayload(
                            action="closed", pull_request=pr, repository=repo_block
                        ),
                        session,
                    )
                    out["prs_resynced"] += int(bool(res.get("withdrawn_links")))
                elif "updated_at" in pr:
                    # Real GitHub list payloads carry updated_at; the guard also keeps
                    # small legacy fixtures from turning reconciliation into a write.
                    res = await handle_pr_edited(
                        PullRequestEventPayload(
                            action="edited", pull_request=pr, repository=repo_block
                        ),
                        session,
                    )
                    # `handle_pr_edited` re-resolves the PR's issue references, so
                    # it can REPAIR a link itself -- a PR edited to add `Fixes #94` while
                    # Boardman was down reaches its task here. That is a repair, not a
                    # resync, and counting it as the latter hid the only path that can now
                    # fix a link silently.
                    if (res.get("relink") or {}).get("linked"):
                        out["prs_relinked"] += 1
                        logger.info("reconcile: PR #%s gained an issue link; repaired", num)
                    else:
                        out["prs_resynced"] += int(bool(res.get("updated")))
                continue  # a stable link exists; metadata/state was reconciled above
            if str(pr.get("state") or "open").casefold() != "open" or pr.get("merged"):
                # An unlinked CLOSED pull request is history, not drift. Replaying it
                # through handle_pr_opened manufactures a brand-new task, assigns a QA
                # and parks it at Needs QA for work that shipped long ago — which is
                # exactly how the board filled with tasks named "Merge main into dev"
                # and a merged dependabot bump sitting in Needs QA. Only open PRs
                # still need a task to represent them.
                out["prs_skipped_closed"] = out.get("prs_skipped_closed", 0) + 1
                continue
            res = await handle_pr_opened(
                PullRequestEventPayload(action="opened", pull_request=pr, repository=repo_block),
                session,
                # A sweep is a replay, whatever the action says. This PR has been open for
                # a while and the task behind it can be anywhere; repairing the link must
                # not also drag it back to Assigned or Needs QA.
                is_replay=True,
            )
            if res.get("linked") or res.get("plaky_task_id"):
                out["prs_relinked"] += 1
                logger.info("reconcile: PR #%s was unlinked; repaired", num)
        except Exception as e:  # noqa: BLE001 - observability failure must not affect the request
            log_degraded(logger, f"reconcile_repo: reconciling PR #{num}", e)
            out["errors"].append(f"PR #{num}: {type(e).__name__}: {e}"[:200])

    out["ok"] = not out["errors"]
    return out
