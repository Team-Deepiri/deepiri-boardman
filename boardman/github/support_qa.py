"""GitHub support-team roster helpers for QA automation."""

from __future__ import annotations

from boardman.github.team_roster import get_cached_support_team_roster
from boardman.settings import settings


def support_team_logins_casefold() -> set[str]:
    """Lowercased GitHub logins for `GITHUB_SUPPORT_TEAM` (empty if PAT/roster unavailable)."""
    data = get_cached_support_team_roster(settings.github_support_team)
    if not data.get("ok"):
        return set()
    out: set[str] = set()
    for m in data.get("members") or []:
        if isinstance(m, dict):
            lg = m.get("login")
            if isinstance(lg, str) and lg.strip():
                out.add(lg.strip().casefold())
    return out
