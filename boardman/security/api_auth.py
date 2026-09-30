"""Shared bearer-token auth for privileged Boardman API routes.

The agent API is reachable from a browser SPA (`boardman-ui`) that cannot hold a
secret, so the read-mostly UI routes intentionally stay unauthenticated. The
routes guarded here are a different category: they spend LLM budget, read or
write caller-supplied API keys (BYOK), or drive the *server's own* GitHub
credentials to open pull requests. Those must never be reachable anonymously,
so they require a bearer token.

Pattern matches `boardman/routes/assignment.py::_require_internal`: fail closed
when no secret is configured, and compare in constant time.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from boardman.settings import settings


def _configured_secret() -> str:
    """Token callers must present. Falls back to the existing worker secret so a
    deployment that already set one does not silently end up unprotected."""
    return (settings.boardman_api_token or settings.worker_internal_secret or "").strip()


def internal_auth_ok(authorization: str | None) -> bool:
    """Non-raising form of :func:`require_internal_auth`.

    For routes the browser SPA legitimately has to reach (agent chat) but which
    expose a privilege *inside* the request body rather than behind the route --
    ``allow_writes`` hands the agent Plaky mutation tools. Those cannot be gated
    with a dependency, because the route itself has to stay open; instead the
    privilege is downgraded when this returns False.
    """
    secret = _configured_secret()
    if not secret:
        return False
    presented = (authorization or "").strip()
    if not presented.lower().startswith("bearer "):
        return False
    token = presented[len("bearer ") :].strip()
    return len(token) == len(secret) and hmac.compare_digest(token, secret)


def require_internal_auth(authorization: str | None = Header(None)) -> None:
    """FastAPI dependency: 401 unless the caller presents the internal bearer token.

    Raises 404 (not 401) when no secret is configured at all, so a
    misconfigured deployment does not advertise that the route exists.
    """
    secret = _configured_secret()
    if not secret:
        raise HTTPException(status_code=404, detail="internal API not configured")

    if not internal_auth_ok(authorization):
        raise HTTPException(status_code=401, detail="invalid authorization")
