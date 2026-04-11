"""FastAPI dependencies: agent route rate limits."""

from __future__ import annotations

from fastapi import HTTPException, Request

from boardman.ratelimit.leaky_bucket import get_agent_leaky_limiter
from boardman.settings import settings


def _client_key(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


async def require_agent_rate_limit(request: Request) -> None:
    if not settings.agent_rate_limit_enabled:
        return
    limiter = await get_agent_leaky_limiter()
    if limiter is None:
        return
    if not await limiter.try_acquire(_client_key(request)):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded (leaky bucket). Slow down and retry shortly.",
        )
