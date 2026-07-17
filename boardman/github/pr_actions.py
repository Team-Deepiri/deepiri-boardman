"""GitHub write actions for PR-time QA assignment: @mention comment + reviewer request.

Both are best-effort: a read-only PAT gets HTTP 403 — we log ONE clear hint about the
missing scope and carry on, because the Plaky side of the assignment must still happen.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from boardman.settings import settings

_log = logging.getLogger(__name__)

_SCOPE_HINT = (
    "GITHUB_PAT lacks write access (needs Issues: write + Pull requests: write on the repo/org) — "
    "QA was still assigned in Plaky, but the GitHub @mention/reviewer request was skipped."
)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.github_pat}",
        "Accept": "application/vnd.github+json",
    }


async def comment_on_pr(full_name: str, pr_number: int, body: str) -> dict[str, Any]:
    """POST an issue comment on the PR (PRs share the issues comment API)."""
    if not (settings.github_pat or "").strip():
        return {"ok": False, "skipped": True, "message": "GITHUB_PAT not configured"}
    url = f"https://api.github.com/repos/{full_name}/issues/{pr_number}/comments"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, headers=_headers(), json={"body": body})
    except Exception as e:  # noqa: BLE001 — network failure must not break the webhook
        _log.warning("pr comment on %s#%s failed: %s", full_name, pr_number, e)
        return {"ok": False, "message": str(e)}
    if r.status_code in (401, 403, 404):
        _log.warning("pr comment on %s#%s -> HTTP %s. %s", full_name, pr_number, r.status_code, _SCOPE_HINT)
        return {"ok": False, "status": r.status_code, "message": _SCOPE_HINT}
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
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, headers=_headers(), json={"reviewers": logins})
    except Exception as e:  # noqa: BLE001
        _log.warning("reviewer request on %s#%s failed: %s", full_name, pr_number, e)
        return {"ok": False, "message": str(e)}
    if r.status_code in (401, 403, 404):
        _log.warning(
            "reviewer request on %s#%s -> HTTP %s. %s", full_name, pr_number, r.status_code, _SCOPE_HINT
        )
        return {"ok": False, "status": r.status_code, "message": _SCOPE_HINT}
    if r.status_code == 422:
        # e.g. reviewer == PR author, or not a collaborator — comment already carries the @mention.
        _log.info("reviewer request on %s#%s -> 422 (%s)", full_name, pr_number, r.text[:120])
        return {"ok": False, "status": 422, "message": "GitHub refused reviewer (author/permissions)"}
    return {"ok": 200 <= r.status_code < 300, "status": r.status_code}
