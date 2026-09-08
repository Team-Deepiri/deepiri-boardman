"""The one seam that decides how Boardman authenticates to the GitHub API.

Every GitHub helper used to build its own ``{"Authorization": f"Bearer {settings.github_pat}"}``
header inline and its own ``if not settings.github_pat`` guard. Migrating to a GitHub App
meant either editing ~25 files or introducing one seam; this is the seam.

- ``github_auth_header()`` — the Authorization + Accept headers to send. Async because
  GitHub App mode may need to mint an installation token.
- ``github_auth_available()`` — replaces the old ``if not settings.github_pat`` guard:
  "does Boardman have *any* usable GitHub credential right now?"

``GITHUB_AUTH_MODE`` selects the behavior:

- ``pat``        — the PAT, exactly as before.
- ``github_app`` — a GitHub App installation token; comments post as ``boardman[bot]``.
- ``both``       — prefer the App token; if minting it fails, fall back to the PAT. This
  is a cutover safety net, not a permanent mode. Note the fallback is on *mint* failure
  only — a per-request 401 is not retried here, because the seam never sees the response.
"""

from __future__ import annotations

import logging

from boardman.github.app_auth import (
    GitHubAppAuthError,
    get_installation_token,
    get_installation_token_sync,
)
from boardman.settings import settings

_log = logging.getLogger(__name__)

_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"


def _mode() -> str:
    return (settings.github_auth_mode or "pat").strip().lower()


def _pat() -> str:
    return (settings.github_pat or "").strip()


def _app_configured() -> bool:
    return bool(
        (settings.github_app_id or "").strip()
        and (settings.github_app_installation_id or "").strip()
        and (settings.github_app_private_key or "").strip()
    )


def github_auth_available() -> bool:
    """True when Boardman has a usable GitHub credential under the current auth mode.

    Drop-in replacement for the old ``if not (settings.github_pat or "").strip()`` guard.
    """
    mode = _mode()
    if mode == "github_app":
        return _app_configured()
    if mode == "both":
        return _app_configured() or bool(_pat())
    return bool(_pat())


def _pat_header() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_pat()}",
        "Accept": _ACCEPT,
        "X-GitHub-Api-Version": _API_VERSION,
    }


async def github_auth_header() -> dict[str, str]:
    """Return the Authorization + Accept headers for a GitHub API call.

    Honors ``GITHUB_AUTH_MODE``. In ``pat`` mode this is a pure string format with no
    I/O; in App modes it may mint/refresh an installation token (cached in
    ``app_auth``).
    """
    mode = _mode()

    if mode == "pat":
        return _pat_header()

    if mode in ("github_app", "both"):
        try:
            token = await get_installation_token()
            return {
                "Authorization": f"Bearer {token}",
                "Accept": _ACCEPT,
                "X-GitHub-Api-Version": _API_VERSION,
            }
        except GitHubAppAuthError:
            if mode == "both" and _pat():
                _log.warning(
                    "github_auth: App token mint failed in 'both' mode, falling back to PAT",
                    exc_info=True,
                )
                return _pat_header()
            raise

    # Unknown mode: readiness.py FAILs on this, but at runtime the safest behavior is the
    # historical one rather than a hard crash.
    _log.warning("github_auth: unrecognized GITHUB_AUTH_MODE=%r, using PAT", mode)
    return _pat_header()


def github_auth_header_sync() -> dict[str, str]:
    """Blocking variant of :func:`github_auth_header` for the non-async call sites
    (the team-roster sync path). Must not be called from inside a running event loop.
    """
    mode = _mode()
    if mode == "pat":
        return _pat_header()
    if mode in ("github_app", "both"):
        try:
            token = get_installation_token_sync()
            return {
                "Authorization": f"Bearer {token}",
                "Accept": _ACCEPT,
                "X-GitHub-Api-Version": _API_VERSION,
            }
        except GitHubAppAuthError:
            if mode == "both" and _pat():
                _log.warning(
                    "github_auth: App token mint failed in 'both' mode (sync), falling back to PAT",
                    exc_info=True,
                )
                return _pat_header()
            raise
    _log.warning("github_auth: unrecognized GITHUB_AUTH_MODE=%r (sync), using PAT", mode)
    return _pat_header()
