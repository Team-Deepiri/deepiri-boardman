"""Support team membership checker for GitHub PR review validation."""

from __future__ import annotations

import logging
from functools import lru_cache

from boardman.github.team_roster import get_cached_support_team_roster

_log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_support_member_logins() -> set[str]:
    """Cache support team GitHub logins for the session."""
    from boardman.settings import settings

    team_spec = settings.github_support_team
    roster = get_cached_support_team_roster(team_spec)
    if not roster.get("ok"):
        _log.warning("Could not load support team roster: %s", roster.get("message"))
        return set()

    logins: set[str] = set()
    for member in roster.get("members") or []:
        if isinstance(member, dict):
            login = member.get("login")
            if login:
                logins.add(login.lower())
    return logins


def is_support_member(github_login: str) -> bool:
    """Check if a GitHub login is a member of the support team."""
    if not github_login:
        return False
    return github_login.lower() in _get_support_member_logins()


def clear_support_member_cache() -> None:
    """Clear the cached support member logins."""
    _get_support_member_logins.cache_clear()
