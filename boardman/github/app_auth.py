"""Mint and cache GitHub App installation access tokens.

A GitHub App does not get one static token the way a PAT is one static string. It holds
a private key, signs a short-lived JWT with it (RS256, 10-minute max lifetime per
GitHub's own limit), and exchanges that JWT for an *installation access token* that is
valid for one hour. That installation token is what actually authenticates API calls,
and it has to be refreshed before it expires.

This module is the one place that does the mint + exchange, and it caches the result
in-process until shortly before expiry so a burst of API calls shares a single token
instead of each minting its own. Concurrency is guarded by an ``asyncio.Lock`` so a
cache miss under load mints exactly once, the same shape ``boardman/llm/factory.py``
uses for chat-model caching.

Only ``get_installation_token()`` is public. The header seam in
``boardman/github/auth.py`` is the only caller.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx
import jwt

from boardman.github.http import shared_github_client
from boardman.settings import settings

# GitHub rejects an App JWT whose lifetime is over 10 minutes. 9 minutes leaves headroom
# for clock skew between here and GitHub without bumping into that ceiling.
_JWT_TTL_SECONDS = 9 * 60
# Installation tokens last an hour. Refresh 5 minutes early so an in-flight request never
# carries a token that expires mid-call.
_REFRESH_MARGIN_SECONDS = 5 * 60

_lock = asyncio.Lock()


@dataclass
class _CachedToken:
    token: str
    # Absolute epoch seconds at which we should stop handing this token out.
    good_until: float


# Keyed by installation id so a config change (or a test monkeypatching settings) does
# not keep serving a token minted for the previous installation.
_cache: dict[str, _CachedToken] = {}


class GitHubAppAuthError(RuntimeError):
    """Raised when an installation token cannot be minted (missing config or API error)."""


def _mint_app_jwt() -> str:
    app_id = (settings.github_app_id or "").strip()
    private_key = (settings.github_app_private_key or "").strip()
    # CI secret stores frequently hold the PEM as a single line with literal "\n"
    # sequences (a multi-line value does not survive a .env round-trip). Restore the
    # real newlines; a PEM that already has them is unaffected.
    if "\\n" in private_key and "\n" not in private_key:
        private_key = private_key.replace("\\n", "\n")
    if not app_id or not private_key:
        raise GitHubAppAuthError(
            "github_auth_mode needs a GitHub App but github_app_id / "
            "github_app_private_key are not set"
        )
    now = int(time.time())
    payload = {
        # iat backdated 60s: GitHub rejects a JWT whose iat is in the future relative to
        # its own clock, and a small negative skew here is common.
        "iat": now - 60,
        "exp": now + _JWT_TTL_SECONDS,
        "iss": app_id,
    }
    try:
        return jwt.encode(payload, private_key, algorithm="RS256")
    except Exception as exc:  # noqa: BLE001 - surface any key-parse failure as our error
        raise GitHubAppAuthError(f"could not sign the GitHub App JWT: {exc}") from exc


async def _exchange_jwt_for_installation_token(app_jwt: str) -> _CachedToken:
    installation_id = (settings.github_app_installation_id or "").strip()
    if not installation_id:
        raise GitHubAppAuthError("github_app_installation_id is not set")
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with shared_github_client() as client:
        resp = await client.post(url, headers=headers)
    try:
        body_json = resp.json()
    except ValueError:
        body_json = {}
    # GitHub returns expires_at as an ISO-8601 string ~1h out. Rather than parse it and
    # trust two clocks, _parse_exchange_response treats the token as good for an hour from
    # now minus the margin — strictly more conservative than GitHub's own expiry.
    return _parse_exchange_response(resp.status_code, resp.text, body_json)


async def get_installation_token() -> str:
    """Return a cached, auto-refreshed GitHub App installation access token.

    Raises ``GitHubAppAuthError`` if the App is not fully configured or GitHub refuses
    the exchange.
    """
    installation_id = (settings.github_app_installation_id or "").strip()
    cached = _cache.get(installation_id)
    if cached and cached.good_until > time.time():
        return cached.token

    async with _lock:
        # Re-check inside the lock: a concurrent caller may have just minted one.
        cached = _cache.get(installation_id)
        if cached and cached.good_until > time.time():
            return cached.token
        fresh = await _exchange_jwt_for_installation_token(_mint_app_jwt())
        _cache[installation_id] = fresh
        return fresh.token


def _parse_exchange_response(status_code: int, body_text: str, body_json: dict) -> _CachedToken:
    if status_code != 201:
        raise GitHubAppAuthError(
            f"installation token exchange failed: {status_code} {body_text[:200]}"
        )
    token = body_json.get("token")
    if not token:
        raise GitHubAppAuthError("installation token exchange returned no token")
    good_until = time.time() + 3600 - _REFRESH_MARGIN_SECONDS
    return _CachedToken(token=token, good_until=good_until)


def get_installation_token_sync() -> str:
    """Blocking variant of :func:`get_installation_token` for the few non-async call
    sites (the team-roster sync path). Serves the shared cache; mints with a one-shot
    ``httpx.Client`` rather than the async pool. Never call from inside an event loop.
    """
    installation_id = (settings.github_app_installation_id or "").strip()
    cached = _cache.get(installation_id)
    if cached and cached.good_until > time.time():
        return cached.token
    if not installation_id:
        raise GitHubAppAuthError("github_app_installation_id is not set")
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {_mint_app_jwt()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, headers=headers)
    try:
        body_json = resp.json()
    except ValueError:
        body_json = {}
    fresh = _parse_exchange_response(resp.status_code, resp.text, body_json)
    _cache[installation_id] = fresh
    return fresh.token


def _clear_cache_for_tests() -> None:
    """Drop every cached token. Used by tests; not part of the runtime path."""
    _cache.clear()
