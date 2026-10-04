"""Map a GitHub profile to a workspace user id (Plaky or ClickUp).

The logic is provider-neutral: the explicit roster mapping (`team_assignments` member overrides)
wins, then an exact GitHub username stored on the provider user, then the conservative fuzzy
matcher. Only the list of workspace users comes from the provider, so it is passed in as a client.
"""

from __future__ import annotations

import logging
from typing import Any

from boardman.assignment.identity_match import best_plaky_match_for_github
from boardman.task_provider import get_task_client

_log = logging.getLogger(__name__)


def github_actor_dict(login: str, *, name: str = "", email: str = "") -> dict[str, Any]:
    return {
        "login": (login or "").strip(),
        "name": (name or "").strip(),
        "email": (email or "").strip(),
    }


def github_actor_payload(user: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize GitHub ``user`` objects from webhooks into the shape expected by identity matching."""
    if not isinstance(user, dict):
        return github_actor_dict("")
    return github_actor_dict(
        str(user.get("login") or ""),
        name=str(user.get("name") or ""),
        email=str(user.get("email") or ""),
    )


async def resolve_github_user(
    gh: dict[str, Any],
    client: Any,
    *,
    min_score: int = 640,
    ambiguity_margin: int = 45,
) -> str | None:
    """Map a GitHub profile (login, optional name/email from a webhook) to a user id of ``client``.

    ``client`` is any task-provider client with ``list_workspace_users()``.

    1) The explicit roster mapping wins: member overrides exist precisely for accounts the fuzzy
       matcher cannot bridge (the login and the workspace email share nothing).
    2) Then a GitHub username stored on the workspace user (exact, case-insensitive).
    3) Then ``best_plaky_match_for_github`` (email, display name and login-token heuristics, with
       conservative ambiguity handling).
    """
    login = str(gh.get("login") or "").strip()
    if not login:
        return None
    want = login.casefold()

    try:
        from boardman.assignment.config import load_team_assignments

        for m in load_team_assignments().members:
            gl = (getattr(m, "github_login", "") or "").strip().casefold()
            mid = (getattr(m, "id", "") or "").strip()
            if gl and mid and gl == want:
                return mid
    except Exception:  # noqa: BLE001 - roster trouble must never break identity resolution
        _log.warning("roster unavailable during GitHub user resolution", exc_info=True)

    r = await client.list_workspace_users()
    if not r.get("ok"):
        return None
    users: list[dict[str, Any]] = [u for u in (r.get("users") or []) if isinstance(u, dict)]
    for u in users:
        uid = str(u.get("id") or "").strip()
        if not uid:
            continue
        linked = u.get("github_login") or u.get("githubLogin") or u.get("githubUsername")
        if isinstance(linked, str) and linked.strip().casefold() == want:
            return uid

    found, reason, _ = best_plaky_match_for_github(
        gh, users, min_score=min_score, ambiguity_margin=ambiguity_margin
    )
    if reason == "matched" and found:
        return str(found).strip() or None
    return None


async def resolve_github_user_to_user_id(
    gh: dict[str, Any], *, min_score: int = 640, ambiguity_margin: int = 45
) -> str | None:
    """:func:`resolve_github_user` against the active task provider's workspace users."""
    return await resolve_github_user(
        gh, get_task_client(), min_score=min_score, ambiguity_margin=ambiguity_margin
    )
