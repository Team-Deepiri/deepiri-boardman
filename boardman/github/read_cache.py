"""Short-TTL cache for read-only GitHub context fetches.

Asking three questions about the same repo re-fetched its metadata, DIRECTION.md, commits,
issues, hotspots and open PRs three times — each one several GitHub round trips before the
model could say anything. Repo context does not change between two questions typed a minute
apart, so the second question should answer from what the first already fetched.

Deliberately narrow:

- **Read-only context only.** Never cache a write, and never cache a PR's review/CI state —
  "is it safe to merge" must read live, because that is exactly what changes while you look
  at it.
- **Short TTL** (``GITHUB_READ_CACHE_TTL_SECONDS``, default 300s). Long enough to cover a
  conversation, short enough that a push shows up while you are still talking about it.
- **Failures are not cached.** A 403 or a network blip must not pin a wrong answer in place
  for five minutes; the next call retries.
- **One fetch per key.** Concurrent callers asking for the same repo wait on the same
  in-flight fetch instead of stampeding the API.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from boardman.settings import settings

logger = logging.getLogger(__name__)

_entries: dict[str, tuple[float, Any]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _ttl() -> float:
    return float(getattr(settings, "github_read_cache_ttl_seconds", 300.0) or 0.0)


def clear_read_cache() -> None:
    _entries.clear()
    _locks.clear()


def cache_stats() -> dict[str, int]:
    now = time.monotonic()
    ttl = _ttl()
    fresh = sum(1 for at, _ in _entries.values() if (now - at) < ttl)
    return {"entries": len(_entries), "fresh": fresh}


async def cached(
    key: str, fetch: Callable[[], Awaitable[Any]], *, ok: Callable[[Any], bool]
) -> Any:
    """Return a cached value for ``key`` or run ``fetch``.

    ``ok`` decides whether a result is worth caching — pass a predicate that returns False
    for error payloads, so a transient failure is retried rather than remembered.
    """
    ttl = _ttl()
    if ttl <= 0:
        return await fetch()

    now = time.monotonic()
    hit = _entries.get(key)
    if hit is not None and (now - hit[0]) < ttl:
        logger.debug("github read cache hit: %s", key)
        return hit[1]

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        # Another caller may have filled it while we waited on the lock.
        hit = _entries.get(key)
        if hit is not None and (time.monotonic() - hit[0]) < ttl:
            return hit[1]
        value = await fetch()
        if ok(value):
            _entries[key] = (time.monotonic(), value)
        return value


def json_ok(payload: Any) -> bool:
    """Cache-worthiness for the tools that return a JSON string."""
    import json

    if not isinstance(payload, str) or not payload:
        return False
    try:
        data = json.loads(payload)
    except ValueError:
        return False
    return bool(isinstance(data, dict) and data.get("ok"))
