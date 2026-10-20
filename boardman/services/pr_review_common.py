"""Review-sync helpers shared by the Plaky and ClickUp handlers (GitHub lookups and roster matching)."""

from __future__ import annotations

import logging

import httpx

from boardman.assignment.config import TeamAssignmentsConfig
from boardman.github.auth import github_auth_available, github_auth_header
from boardman.github.http import github_http_client
from boardman.observability.degradation import log_degraded

_log = logging.getLogger(__name__)


async def pr_author_login(full_name: str, pr_number: int) -> str:
    """The PR's author GitHub login, fetched from the API.

    The `issue_comment` webhook payload does not embed the PR author, but knowing it is
    required to keep the PR author's own comments from reading as "QA started" — they are
    on the support roster more often than not, and their comment must not move a task to
    In QA. One lightweight `/pulls/{n}` call is cheaper than the state corruption it
    prevents. Returns "" if the PR cannot be read (fail-closed: never guess).
    """
    try:
        owner_repo = (full_name or "").strip().strip("/")
        if not owner_repo or "/" not in owner_repo:
            return ""
        from urllib.parse import quote

        owner, repo = owner_repo.split("/", 1)
        url = (
            "https://api.github.com/repos/"
            f"{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{int(pr_number)}"
        )
        r = await github_http_client().get(url, headers=await github_auth_header())
        if r.status_code != 200:
            return ""
        data = r.json()
        user = data.get("user") if isinstance(data, dict) else None
        return str((user or {}).get("login") or "").strip() if isinstance(user, dict) else ""
    except (httpx.HTTPError, ValueError, TypeError):
        return ""


def reviewer_id_from_roster(cfg: TeamAssignmentsConfig, reviewer_login: str) -> str | None:
    if not reviewer_login:
        return None
    # fallback_members too: the bug specialist lives only in the yaml fallback, and her
    # reviews/comments must carry the same authority as any rostered QA's.
    for pool in (cfg.members, getattr(cfg, "fallback_members", []) or []):
        for m in pool:
            gl = (getattr(m, "github_login", None) or "").strip()
            if gl and gl.casefold() == reviewer_login.casefold():
                mid = (getattr(m, "id", None) or "").strip()
                if mid:
                    return mid
    return None


async def failing_required_checks(full_name: str, pr_number: int) -> list[str]:
    """Names of failing check runs on the PR head commit, [] when green or unknowable.

    An approval is a verdict on the code, not on the build. If required checks are red,
    marking the task QA Verified would present broken work as done. API trouble returns
    [] on purpose: absence of the signal is not evidence of failure.
    """
    if not github_auth_available():
        return []
    try:
        from boardman.github.http import github_http_client

        client = github_http_client()
        hdr = await github_auth_header()
        r = await client.get(
            f"https://api.github.com/repos/{full_name}/pulls/{int(pr_number)}", headers=hdr
        )
        if r.status_code != 200:
            return []
        sha = str(((r.json().get("head") or {}) or {}).get("sha") or "")
        if not sha:
            return []
        r2 = await client.get(
            f"https://api.github.com/repos/{full_name}/commits/{sha}/check-runs?per_page=100",
            headers=hdr,
        )
        if r2.status_code != 200:
            return []
        bad: list[str] = []
        for run in r2.json().get("check_runs") or []:
            if not isinstance(run, dict):
                continue
            if str(run.get("status") or "") == "completed" and str(
                run.get("conclusion") or ""
            ).lower() in ("failure", "timed_out", "action_required"):
                bad.append(str(run.get("name") or "check"))
        return bad
    except Exception:  # noqa: BLE001 - graceful degradation
        log_degraded(_log, "failing_required_checks: GET /commits/{ref}/check-runs")
        return []


async def current_commit_count(full_name: str, pr_number: int) -> int | None:
    """Live commit count on the PR right now — None when unknowable (no PAT, API
    trouble), so callers skip stamping rather than recording a wrong baseline."""
    if not github_auth_available():
        return None
    try:
        from boardman.github.http import github_http_client

        client = github_http_client()
        hdr = await github_auth_header()
        r = await client.get(
            f"https://api.github.com/repos/{full_name}/pulls/{int(pr_number)}", headers=hdr
        )
        if r.status_code != 200:
            return None
        commits = r.json().get("commits")
        return int(commits) if isinstance(commits, int) else None
    except Exception:  # noqa: BLE001 - a missing baseline degrades to "can't escalate yet"
        return None


async def pr_is_merged(full_name: str, pr_number: int) -> bool:
    """Live merged state on the PR right now — False (not "unknown") on any API trouble.

    "Resume work" comment handling reads the task's post-review status and, if it
    matches an approved/changes-requested verdict, moves the task to In Progress. That
    read can race a `pull_request.closed(merged=true)` webhook delivered around the same
    time: if this comment webhook is processed first (or the Plaky write from the merge
    handler hasn't landed yet), the stale pre-merge status still matches and the task
    gets bounced back to In Progress right after (or just before) it was set Completed,
    with nothing downstream ever correcting it. Checking the PR's live merged state
    directly — not the comment payload, which does not carry it — closes that race.
    """
    if not github_auth_available():
        return False
    try:
        from boardman.github.http import github_http_client

        client = github_http_client()
        hdr = await github_auth_header()
        r = await client.get(
            f"https://api.github.com/repos/{full_name}/pulls/{int(pr_number)}", headers=hdr
        )
        if r.status_code != 200:
            return False
        return bool(r.json().get("merged"))
    except Exception:  # noqa: BLE001 - unknowable degrades to "not merged" (safe default:
        # the branch still runs, matching today's behavior when this check can't run)
        return False
