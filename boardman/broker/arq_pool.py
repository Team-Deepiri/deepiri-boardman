"""Shared arq Redis pool for enqueueing jobs from the API process."""

from __future__ import annotations

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from boardman.settings import settings

_arq_pool: ArqRedis | None = None


async def get_arq_pool() -> ArqRedis:
    global _arq_pool
    url = (settings.redis_url or "").strip()
    if not url:
        raise RuntimeError("REDIS_URL is not configured")
    if _arq_pool is None:
        _arq_pool = await create_pool(RedisSettings.from_dsn(url))
    return _arq_pool


async def close_arq_pool() -> None:
    global _arq_pool
    if _arq_pool is not None:
        await _arq_pool.close(close_connection_pool=True)
        _arq_pool = None


def reset_arq_pool_for_tests() -> None:
    global _arq_pool
    _arq_pool = None
