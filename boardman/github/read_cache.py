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
# repo -> how many times it has been invalidated. A fetch that started before a push and
# returns after it would otherwise write the PRE-push value back into an empty cache, and
# the invalidation would have achieved nothing: the key was not in _entries yet when the
# purge ran, so there was nothing to drop.
_epochs: dict[str, int] = {}
_hits = 0
_misses = 0
_coalesced = 0


def _ttl() -> float:
    return float(getattr(settings, "github_read_cache_ttl_seconds", 300.0) or 0.0)


def clear_read_cache() -> None:
    global _hits, _misses, _coalesced
    _entries.clear()
    _locks.clear()
    _epochs.clear()
    _hits = 0
    _misses = 0
    _coalesced = 0


def _key_repo(key: str) -> str:
    """The owner/repo a cache key belongs to, casefolded, or "".

    Every namespace puts the slug immediately after its prefix and then ends, or continues
    with ``:`` (a path, a limit) or ``@`` (a ref):

        structure:team-deepiri/boardman        planning:team-deepiri/boardman:20
        defects:team-deepiri/boardman          repo-identity:Team-Deepiri/boardman
        repo-tree:Team-Deepiri/boardman@main   hotspots:Team-Deepiri/boardman@main:15
        file:Team-Deepiri/boardman@main:README.md

    Half the writers lowercase the slug and half do not, so the comparison has to
    casefold both sides. A purge that misses half the keys leaves a MIXED-AGE context,
    which is worse than a uniformly stale one.
    """
    _, _, rest = (key or "").partition(":")
    if not rest:
        return ""
    slug = rest.split("@", 1)[0].split(":", 1)[0].strip().casefold()
    if slug.count("/") == 1:
        return slug
    # A BARE repo name is a supported tool argument, and the key is built from whatever
    # the caller passed -- so `defects:boardman` and `planning:boardman:20` were never
    # purged, and the epoch guard that discards a fetch started before the event could
    # not fire for them either. Resolve it the same way the tools do.
    if slug and "/" not in slug:
        try:
            from boardman.assignment.qa_picker import ensure_github_owner_repo

            resolved = ensure_github_owner_repo(slug)
        except Exception:  # noqa: BLE001 - a cache key must never break a purge
            return ""
        return str(resolved or "").strip().casefold()
    return ""


def invalidate_repo(full_name: str) -> int:
    """Drop every cached read for one repo. Returns how many entries went.

    Called when GitHub tells us the repo changed. Purging one repo rather than the whole
    cache means a push to one project does not make every other project's next question
    slow again.
    """
    want = (full_name or "").strip().casefold()
    if "/" not in want:
        return 0
    # Bumped before anything is dropped, and even when nothing is cached — the in-flight
    # case is exactly the one where `doomed` is empty.
    _epochs[want] = _epochs.get(want, 0) + 1
    doomed = [k for k in _entries if _key_repo(k) == want]
    for k in doomed:
        _entries.pop(k, None)
    # Locks for keys nobody is waiting on; a held lock is left alone so an in-flight
    # fetch is not orphaned, and it will be re-created on the next miss anyway.
    for k in [k for k in list(_locks) if _key_repo(k) == want]:
        lock = _locks.get(k)
        if lock is not None and not lock.locked():
            _locks.pop(k, None)
    if doomed:
        from boardman.observability.counters import bump

        bump("cache.github_read.invalidated", len(doomed))
        logger.info("github read cache: dropped %d entries for %s", len(doomed), full_name)
    return len(doomed)


def cache_stats() -> dict[str, int]:
    now = time.monotonic()
    ttl = _ttl()
    fresh = sum(1 for at, _ in _entries.values() if (now - at) < ttl)
    return {
        "entries": len(_entries),
        "fresh": fresh,
        "hits": _hits,
        "misses": _misses,
        "coalesced": _coalesced,
    }


async def cached(
    key: str, fetch: Callable[[], Awaitable[Any]], *, ok: Callable[[Any], bool]
) -> Any:
    """Return a cached value for ``key`` or run ``fetch``.

    ``ok`` decides whether a result is worth caching — pass a predicate that returns False
    for error payloads, so a transient failure is retried rather than remembered.
    """
    from boardman.observability.counters import bump, cache_hit, cache_miss

    global _hits, _misses, _coalesced
    ttl = _ttl()
    if ttl <= 0:
        _misses += 1
        cache_miss("github_read")
        return await fetch()

    now = time.monotonic()
    hit = _entries.get(key)
    if hit is not None and (now - hit[0]) < ttl:
        _hits += 1
        cache_hit("github_read")
        logger.debug("github read cache hit: %s", key)
        return hit[1]

    lock = _locks.setdefault(key, asyncio.Lock())
    if lock.locked():
        _coalesced += 1
        bump("cache.github_read.coalesced")
    async with lock:
        # Another caller may have filled it while we waited on the lock.
        hit = _entries.get(key)
        if hit is not None and (time.monotonic() - hit[0]) < ttl:
            _hits += 1
            cache_hit("github_read")
            return hit[1]
        _misses += 1
        cache_miss("github_read")
        repo = _key_repo(key)
        epoch_before = _epochs.get(repo, 0)
        value = await fetch()
        if _epochs.get(repo, 0) != epoch_before:
            # The repo changed while this was in flight. The value is already history;
            # return it to this caller but never let it become the cached answer.
            bump("cache.github_read.discarded_stale_fetch")
            return value
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
