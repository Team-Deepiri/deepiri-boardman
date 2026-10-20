"""PR-sync helpers shared by the Plaky and ClickUp handlers (no provider-specific code)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from boardman.github.webhooks import PullRequestEventPayload
from boardman.services.pr_task_registry import (
    _BRANCH_REF_LINK_SOURCE,
    _TITLE_REF_LINK_SOURCE,
)

# Where QA has RULED. Un-asking for a review must not overwrite one of these; anything
# earlier is a position the request itself was about, and moving it is the point.
QA_VERDICT_INTENTS = ("github_pr_review_approved", "workflow_completed")


def issue_link_source(
    issue_number: int, body_written: Sequence[int], written: Sequence[int]
) -> str:
    """Which kind of statement links this PR to that issue.

    Three kinds, and merge treats them differently. The description is the one GitHub acts
    on, so it is the only one that means "merging this finishes that issue". A title
    keyword and a branch convention are both real links -- a person wrote the title, and
    this team names branches after issues on purpose -- but GitHub leaves the issue open
    after such a merge, and a board saying Completed while the issue is still open is a
    state the issue's own events go on to contradict.
    """
    n = int(issue_number)
    if n in body_written:
        return "issue_keyword"
    if n in written:
        return _TITLE_REF_LINK_SOURCE
    return _BRANCH_REF_LINK_SOURCE


async def ensure_links_live(payload: PullRequestEventPayload, session: AsyncSession) -> None:
    """Any PR event showing it OPEN un-withdraws its links.

    Called from every handler that receives a PullRequestEventPayload. The comment and
    review handlers cannot: their payloads carry no PR state to trust. They read through
    `distinct_task_ids_for_pr`, which filters on the link SOURCE rather than on
    withdrawn_at, so a stale withdrawal does not blind them either way.


    `withdrawn_at` means "this PR was closed without merging". Seeing the PR open again is
    proof that is stale, whatever the event was. Keying the recovery on `reopened` alone
    was too narrow: that delivery can be lost, and the poller's closed-PR memory is
    in-process, so a restart means it never sends one. Since the resolver started honouring
    the flag, either of those left the PR resolving to zero tasks forever -- reviews,
    comments, pushes and label changes all silently stopped.
    """
    state = str(getattr(payload.pull_request, "state", "") or "").casefold()
    if state != "open" or bool(getattr(payload.pull_request, "merged", False)):
        # Explicitly open, and not merged. Defaulting a MISSING state to "open" made the
        # slim events-feed payload shape look open, and a delivery that predates the close
        # (retried job, out-of-order webhook) would then leave a closed PR holding a live
        # link -- which blocks merge-gated completion of that task for good, since nothing
        # re-withdraws it.
        return
    if str(getattr(payload.pull_request, "closed_at", "") or "").strip():
        return
    if str(getattr(payload.pull_request, "merged_at", "") or "").strip():
        return
    # And the residual case those two do NOT catch: a delivery built BEFORE the close.
    # GitHub sets state and closed_at together, so a snapshot from before it says "open"
    # with closed_at null and passes every field test there is. What separates it from a
    # genuine reopen is WHEN it was built: `updated_at` on a stale delivery predates the
    # withdrawal, and on a real reopen follows it. Reviving on a stale one is permanent --
    # nothing withdraws those links a second time, and has_any_open_pr_for_task then
    # blocks merge-gated completion of that task for good.
    from boardman.services.pr_task_registry import revive_pr_links

    if await revive_pr_links(
        session,
        github_repo=payload.repository.name,
        github_pr_number=payload.pull_request.number,
        not_before=str(getattr(payload.pull_request, "updated_at", "") or "").strip(),
    ):
        await session.commit()


def member_by_name(cfg: Any, name: str) -> Any | None:
    """Resolve a policy role (e.g. the bug specialist) by display name or GitHub login.

    Checks the live roster first, then the yaml fallback list — the specialist may not be
    on the GitHub support team the live roster is built from (Hameeda is exactly this
    case), and a policy the employer stated must not silently stop applying because of
    team-membership drift.
    """
    want = (name or "").strip().casefold()
    if not want:
        return None
    for pool in (cfg.members, getattr(cfg, "fallback_members", []) or []):
        for m in pool:
            display = (getattr(m, "display", "") or "").strip().casefold()
            login = (getattr(m, "github_login", "") or "").strip().casefold()
            if want in (display, login):
                return m
    return None


# More than this many commits pushed since the last QA verdict escalates past
# "Revisions In Progress" straight to "Needs QA Again" — the developer clearly isn't
# just fixing the one thing QA flagged anymore.
COMMITS_SINCE_REVIEW_ESCALATION_THRESHOLD = 5


async def prs_for_commit_sha(repo_full: str, sha: str) -> list[int]:
    """GitHub's own "which PRs contain this commit" lookup — used to map a
    deployment's sha back to the PR(s) it shipped without guessing from the ref."""
    if not sha:
        return []
    from boardman.github.http import shared_github_client
    from boardman.github.repo_fetch import github_request

    async with shared_github_client() as client:
        r = await github_request(client, f"/repos/{repo_full}/commits/{sha}/pulls")
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except Exception:  # noqa: BLE001 - a malformed response yields "no PRs found", not a crash
        return []
    if not isinstance(data, list):
        return []
    out: list[int] = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("number"), int):
            out.append(item["number"])
    return out
