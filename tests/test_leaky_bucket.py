"""Leaky-bucket limiter (memory)."""

from __future__ import annotations

import asyncio

import pytest

from boardman.ratelimit.leaky_bucket import MemoryLeakyBucket, reset_agent_leaky_limiter_for_tests


@pytest.fixture(autouse=True)
def _reset_global_limiter():
    reset_agent_leaky_limiter_for_tests()
    yield
    reset_agent_leaky_limiter_for_tests()


@pytest.mark.asyncio
async def test_memory_leaky_bucket_allows_burst_then_blocks():
    b = MemoryLeakyBucket(capacity=3.0, leak_per_second=1000.0)
    assert await b.try_acquire("a")
    assert await b.try_acquire("a")
    assert await b.try_acquire("a")
    assert not await b.try_acquire("a")


@pytest.mark.asyncio
async def test_memory_leaky_bucket_leaks():
    b = MemoryLeakyBucket(capacity=2.0, leak_per_second=50.0)
    assert await b.try_acquire("k")
    assert await b.try_acquire("k")
    assert not await b.try_acquire("k")
    await asyncio.sleep(0.05)
    assert await b.try_acquire("k")
