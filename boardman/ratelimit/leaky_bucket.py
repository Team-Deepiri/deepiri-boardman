"""Leaky-bucket limiter: in-memory (single process) or Redis (multi-instance)."""

from __future__ import annotations

import asyncio
import time
from typing import Optional, Protocol

import redis.asyncio as aioredis

_LEAKY_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local leak = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local h = redis.call('HMGET', key, 'w', 't')
local function num(x)
  if not x or x == false then return 0 end
  return tonumber(x) or 0
end
local w = num(h[1])
local t = now
if h[2] and h[2] ~= false then
  local tt = tonumber(h[2])
  if tt then t = tt end
end
local dt = now - t
if dt > 0 then
  w = math.max(0, w - dt * leak)
end
if w + 1 > capacity then
  redis.call('HMSET', key, 'w', tostring(w), 't', tostring(now))
  redis.call('EXPIRE', key, 7200)
  return 0
end
w = w + 1
redis.call('HMSET', key, 'w', tostring(w), 't', tostring(now))
redis.call('EXPIRE', key, 7200)
return 1
"""


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


class RedisLeakyBucket:
    """Distributed leaky bucket (wall-clock time.time() for cross-process consistency)."""

    def __init__(
        self,
        client: aioredis.Redis,
        *,
        capacity: float,
        leak_per_second: float,
        key_prefix: str = "boardman:leaky:agent",
    ) -> None:
        self._r = client
        self._capacity = max(0.01, capacity)
        self._leak = max(1e-6, leak_per_second)
        self._prefix = key_prefix.rstrip(":")
        self._script = client.register_script(_LEAKY_LUA)

    async def try_acquire(self, key: str) -> bool:
        now = time.time()
        k = f"{self._prefix}:{key}"
        n = await self._script(keys=[k], args=[str(self._capacity), str(self._leak), str(now)])
        return int(n) == 1


_agent_limiter: Optional[LeakyAcquire] = None
_agent_limiter_lock = asyncio.Lock()


async def get_agent_leaky_limiter() -> Optional[LeakyAcquire]:
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
        if settings.agent_rate_limit_use_redis and (settings.redis_url or "").strip():
            client = aioredis.from_url(
                settings.redis_url.strip(),
                encoding="utf-8",
                decode_responses=True,
            )
            _agent_limiter = RedisLeakyBucket(client, capacity=cap, leak_per_second=leak)
        else:
            _agent_limiter = MemoryLeakyBucket(capacity=cap, leak_per_second=leak)
    return _agent_limiter


def reset_agent_leaky_limiter_for_tests() -> None:
    global _agent_limiter
    _agent_limiter = None
