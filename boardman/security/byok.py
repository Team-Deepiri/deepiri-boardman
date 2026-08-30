"""Bring-your-own-key: a user can temporarily supply their own LLM provider API key
for one chat session instead of using the shared default (OpenRouter free-tier).

Design:
- Encrypted at rest with Fernet (AES-128-CBC + HMAC, authenticated) using a server-side
  secret (`settings.byok_encryption_key`) that never leaves the server and is never
  derived from anything a client sends.
- Scoped to ONE AgentSession row, not a person — there's no login system in Boardman,
  so "temporary, per-session" is the honest privacy boundary this can offer.
- Time-limited (`byok_key_expires_at`) — a key stops being usable after the TTL even if
  the session itself stays alive, so a forgotten key doesn't sit valid indefinitely.
- Never returned in any API response once stored, and never written to logs — every
  call site here takes/returns the plaintext only long enough to encrypt or use it.

If `BOARDMAN_BYOK_ENCRYPTION_KEY` is not set, the whole feature is off: `is_configured()`
false, and every route/tool call refuses rather than falling back to a weaker default —
storing API keys with no real encryption key is worse than not offering the feature.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken

from boardman.settings import settings

_log = logging.getLogger(__name__)

_ALLOWED_PROVIDERS = {"openai", "anthropic", "openrouter", "gemini"}


def is_configured() -> bool:
    return bool((settings.byok_encryption_key or "").strip())


def _fernet() -> Fernet:
    # Fernet needs a 32-byte urlsafe-base64 key; derive one from whatever secret string
    # is configured so operators can set a plain passphrase in .env rather than having
    # to pre-generate a Fernet-formatted key themselves.
    raw = (settings.byok_encryption_key or "").strip().encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_key(provider: str, api_key: str) -> str:
    """Ciphertext to store in AgentSession.byok_key_encrypted. Raises if not configured —
    callers must check is_configured() first and refuse the request instead."""
    if not is_configured():
        raise RuntimeError("BYOK encryption key is not configured on this server")
    token = _fernet().encrypt(api_key.strip().encode("utf-8"))
    return token.decode("ascii")


def decrypt_key(ciphertext: str) -> str | None:
    """Plaintext key, or None if it can't be decrypted (wrong/rotated server secret,
    corrupted value) — callers treat that identically to "no key configured", never as
    an error surfaced to the end user."""
    if not is_configured() or not ciphertext:
        return None
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        _log.warning("BYOK key could not be decrypted (server secret rotated?)")
        return None


def normalize_provider(provider: str) -> str | None:
    p = (provider or "").strip().lower()
    return p if p in _ALLOWED_PROVIDERS else None


def default_expiry(hours: float | None = None) -> datetime:
    ttl = hours if hours is not None else settings.byok_key_ttl_hours
    return datetime.now(UTC) + timedelta(hours=max(0.25, float(ttl)))


def is_expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return True
    exp = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
    return datetime.now(UTC) >= exp
