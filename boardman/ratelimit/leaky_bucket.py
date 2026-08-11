"""Leaky-bucket limiter: in-memory (single process) or SQLite (multi-instance)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Protocol

from boardman.database.models import AgentRateLimitBucket
from boardman.database.session import async_session


class LeakyAcquire(Protocol):
    async def try_acquire(self, key: str) -> bool: ...


class MemoryLeakyBucket:
    """Leaky bucket using wall-clock monotonic time (single uvicorn worker)."""

    def __init__(self, *, capacity: float, leak_per_second: float) -> None:
        self._capacity = max(0.01, capacity)
        self._leak = max(1e-6, leak_per_second)
        self._lock = asyncio.Lock()
        self._water: dict[str, tuple[float, float]] = {}

    async def try_acquire(self, key: str) -> bool:
        async with self._lock:
            now = time.monotonic()
            water, ts = self._water.get(key, (0.0, now))
            elapsed = now - ts
            water = max(0.0, water - elapsed * self._leak)
            if water + 1.0 > self._capacity + 1e-9:
                self._water[key] = (water, now)
                return False
            self._water[key] = (water + 1.0, now)
            return True


class SqliteLeakyBucket:
    """Distributed leaky bucket using SQLite (`AgentRateLimitBucket`), wall-clock time.time()."""

    def __init__(
        self,
        *,
        capacity: float,
        leak_per_second: float,
        key_prefix: str = "boardman:leaky:agent",
    ) -> None:
        self._capacity = max(0.01, capacity)
        self._leak = max(1e-6, leak_per_second)
        self._prefix = key_prefix.rstrip(":")
        self._serialize = asyncio.Lock()

    async def try_acquire(self, key: str) -> bool:
        full_key = f"{self._prefix}:{key}"
        async with self._serialize:
            async with async_session() as session:
                async with session.begin():
                    row = await session.get(AgentRateLimitBucket, full_key)
                    now = time.time()
                    water, t = (0.0, now)
                    if row is not None:
                        water, t = float(row.water), float(row.ts)
                    dt = now - t
                    if dt > 0:
                        water = max(0.0, water - dt * self._leak)
                    allow = water + 1.0 <= self._capacity + 1e-9
                    if allow:
                        water = water + 1.0
                    if row is None:
                        row = AgentRateLimitBucket(bucket_key=full_key, water=water, ts=now)
                    else:
                        row.water = water
                        row.ts = now
                    row.updated_at = datetime.utcnow()
                    session.add(row)
                    return allow


_agent_limiter: LeakyAcquire | None = None
_agent_limiter_lock = asyncio.Lock()


async def get_agent_leaky_limiter() -> LeakyAcquire | None:
    """Lazy init from settings (call from app lifespan or first request)."""
    global _agent_limiter
    from boardman.settings import settings

    if not settings.agent_rate_limit_enabled:
        return None
    if _agent_limiter is not None:
        return _agent_limiter
    async with _agent_limiter_lock:
        if _agent_limiter is not None:
            return _agent_limiter
        cap = float(settings.agent_rate_limit_capacity)
        leak = float(settings.agent_rate_limit_leak_per_second)
        if settings.agent_rate_limit_use_sqlite:
            _agent_limiter = SqliteLeakyBucket(capacity=cap, leak_per_second=leak)
        else:
            _agent_limiter = MemoryLeakyBucket(capacity=cap, leak_per_second=leak)
    return _agent_limiter


def reset_agent_leaky_limiter_for_tests() -> None:
    global _agent_limiter
    _agent_limiter = None
