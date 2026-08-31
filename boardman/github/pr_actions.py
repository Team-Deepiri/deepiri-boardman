"""GitHub write actions for PR-time QA assignment: @mention comment + reviewer request.

Both are best-effort: a read-only PAT gets HTTP 403 — we log ONE clear hint about the
missing scope and carry on, because the Plaky side of the assignment must still happen.
"""

from __future__ import annotations

import logging
from typing import Any

from boardman.github.http import shared_github_client
from boardman.settings import settings

_log = logging.getLogger(__name__)

_SCOPE_HINT = (
    "GITHUB_PAT lacks write access (needs Issues: write + Pull requests: write on the repo/org) — "
    "QA was still assigned in Plaky, but the GitHub @mention/reviewer request was skipped."
)
_NOT_FOUND_HINT = (
    "target not found on GitHub (the PR/issue number does not exist in this repo). "
    "If it does exist, check that the PAT has read access. QA was still assigned in Plaky."
)


def _failure_hint(status: int) -> str:
    """404 is ambiguous: it means 'no such PR' OR 'token cannot see it'. Saying 'lacks write
    access' for a nonexistent number sends people to re-issue a perfectly good token."""
    return _NOT_FOUND_HINT if status == 404 else _SCOPE_HINT


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.github_pat}",
        "Accept": "application/vnd.github+json",
    }


# Boardman comments on GitHub as the PAT owner — a real teammate account, not a "[bot]"
# login. Without a marker it reads its own QA-assignment comment back through the event
# feed, sees a support-team member commenting on a PR, and moves the task to In QA before
# QA has looked at anything. An HTML comment is invisible in rendered markdown.
BOARDMAN_COMMENT_MARKER = "<!-- boardman:automation -->"


def with_marker(body: str) -> str:
    return body if BOARDMAN_COMMENT_MARKER in body else f"{body}\n\n{BOARDMAN_COMMENT_MARKER}"


def is_boardman_comment(body: str) -> bool:
    return BOARDMAN_COMMENT_MARKER in (body or "")


async def has_qa_assignment_comment(full_name: str, pr_number: int) -> bool:
    """Has Boardman already posted the QA-assignment comment on this PR?

    Used to make a QA-notification retry idempotent: a task can already carry the
    correct QA in Plaky (write succeeded) while the GitHub comment failed independently
    (e.g. the PAT lacked write scope at the time) -- a later retry must post the comment
    exactly once when the scope is fixed, not re-post it on every reconcile pass after.
    """
    if not (settings.github_pat or "").strip():
        return False
    url = f"https://api.github.com/repos/{full_name}/issues/{pr_number}/comments?per_page=100"
    try:
        async with shared_github_client() as client:
            r = await client.get(url, headers=_headers())
    except Exception as e:  # noqa: BLE001 — a failed check must not block a retry
        _log.warning("qa-comment lookup on %s#%s failed: %s", full_name, pr_number, e)
        return False
    if r.status_code != 200:
        return False
    for c in r.json() or []:
        body = str((c or {}).get("body") or "")
        if is_boardman_comment(body) and "QA reviewer" in body:
            return True
    return False


async def comment_on_pr(full_name: str, pr_number: int, body: str) -> dict[str, Any]:
    """POST an issue comment on the PR (PRs share the issues comment API)."""
    if not (settings.github_pat or "").strip():
        return {"ok": False, "skipped": True, "message": "GITHUB_PAT not configured"}
    url = f"https://api.github.com/repos/{full_name}/issues/{pr_number}/comments"
    try:
        async with shared_github_client() as client:
            # follow_redirects=False for a WRITE. The shared pool follows them, and httpx
            # turns a redirected POST into a bodyless GET -- so after a repo rename this
            # fetched the comment list, got 200, and reported a comment it never posted.
            r = await client.post(
                url,
                headers=_headers(),
                json={"body": with_marker(body)},
                follow_redirects=False,
            )
    except Exception as e:  # noqa: BLE001 — network failure must not break the webhook
        _log.warning("pr comment on %s#%s failed: %s", full_name, pr_number, e, exc_info=True)
        return {"ok": False, "message": str(e)}
    if r.status_code in (401, 403, 404):
        hint = _failure_hint(r.status_code)
        _log.warning(
            "pr comment on %s#%s -> HTTP %s. %s", full_name, pr_number, r.status_code, hint
        )
        return {"ok": False, "status": r.status_code, "message": hint}
    return {"ok": 200 <= r.status_code < 300, "status": r.status_code}


async def request_reviewers(full_name: str, pr_number: int, logins: list[str]) -> dict[str, Any]:
    """POST requested reviewers onto the PR (GitHub refuses the PR author as reviewer)."""
    logins = [str(x).strip() for x in logins if str(x).strip()]
    if not logins:
        return {"ok": False, "skipped": True, "message": "no reviewer logins"}
    if not (settings.github_pat or "").strip():
        return {"ok": False, "skipped": True, "message": "GITHUB_PAT not configured"}
    url = f"https://api.github.com/repos/{full_name}/pulls/{pr_number}/requested_reviewers"
    try:
        async with shared_github_client() as client:
            # See `comment_on_pr`: a redirected POST silently becomes a GET.
            r = await client.post(
                url, headers=_headers(), json={"reviewers": logins}, follow_redirects=False
            )
    except Exception as e:  # noqa: BLE001 — network failure must not break the webhook
        _log.warning("reviewer request on %s#%s failed: %s", full_name, pr_number, e, exc_info=True)
        return {"ok": False, "message": str(e)}
    if r.status_code in (401, 403, 404):
        hint = _failure_hint(r.status_code)
        _log.warning(
            "reviewer request on %s#%s -> HTTP %s. %s", full_name, pr_number, r.status_code, hint
        )
        return {"ok": False, "status": r.status_code, "message": hint}
    if r.status_code == 422:
        # e.g. reviewer == PR author, or not a collaborator — comment already carries the @mention.
        _log.info("reviewer request on %s#%s -> 422 (%s)", full_name, pr_number, r.text[:120])
        return {
            "ok": False,
            "status": 422,
            "message": "GitHub refused reviewer (author/permissions)",
        }
    return {"ok": 200 <= r.status_code < 300, "status": r.status_code}
