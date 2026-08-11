"""Optional Redis for the **API / agent** process only (local dev, multi-replica cache).

The SQLite job worker must not rely on this: leave ``AGENT_REDIS_URL`` unset (or empty) for
``boardman-worker``. All helpers no-op when the URL is empty or Redis is unreachable.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from boardman.settings import settings

_log = logging.getLogger(__name__)

_client: Any = None
_lock = asyncio.Lock()
_warned_unreachable = False


def agent_redis_configured() -> bool:
    return bool((settings.agent_redis_url or "").strip())


async def get_agent_redis() -> Any:
    """Return ``redis.asyncio.Redis`` or ``None`` if disabled / unavailable."""
    global _client, _warned_unreachable
    if not agent_redis_configured():
        return None
    if _client is not None:
        return _client
    async with _lock:
        if _client is not None:
            return _client
        try:
            import redis.asyncio as aioredis
        except ImportError:
            if not _warned_unreachable:
                _log.warning("redis package missing; install redis extras for AGENT_REDIS_URL")
                _warned_unreachable = True
            return None
        url = settings.agent_redis_url.strip()
        try:
            r = aioredis.from_url(url, encoding="utf-8", decode_responses=True)
            await r.ping()
            _client = r
            _log.info("Agent Redis cache: connected (%s)", url.split("@")[-1])
            return _client
        except Exception as e:
            if not _warned_unreachable:
                _log.warning("Agent Redis cache: unreachable (%s); using in-process cache only", e)
                _warned_unreachable = True
            return None


async def agent_redis_get_json(key: str) -> dict[str, Any] | None:
    r = await get_agent_redis()
    if r is None:
        return None
    try:
        raw = await r.get(key)
        if not raw:
            return None
        return json.loads(raw) if isinstance(raw, str) else json.loads(str(raw))
    except Exception as e:
        _log.debug("agent redis get %s: %s", key, e)
        return None


async def agent_redis_set_json(key: str, value: dict[str, Any], ttl_seconds: int) -> None:
    r = await get_agent_redis()
    if r is None:
        return
    try:
        ttl = max(1, int(ttl_seconds))
        await r.set(key, json.dumps(value, default=str), ex=ttl)
    except Exception as e:
        _log.debug("agent redis set %s: %s", key, e)


async def aclose_agent_redis() -> None:
    global _client, _warned_unreachable
    if _client is None:
        return
    try:
        await _client.aclose()
    except Exception as e:
        _log.debug("agent redis close: %s", e)
    finally:
        _client = None
        _warned_unreachable = False


def reset_agent_redis_for_tests() -> None:
    """Sync reset for tests (does not close remote connections if loop not running)."""
    global _client, _warned_unreachable
    _client = None
    _warned_unreachable = False
