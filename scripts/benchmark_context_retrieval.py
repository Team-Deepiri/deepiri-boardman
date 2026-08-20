#!/usr/bin/env python3
"""Measure the bounded repo-read cache without requiring GitHub credentials.

Run from the repository root:
    poetry run python scripts/benchmark_context_retrieval.py

This is a deterministic cache benchmark, not a claim about live GitHub latency. It
separates the uncached fetch cost from warm-cache reuse and concurrent coalescing.
"""

from __future__ import annotations

import asyncio
import json
import time

from boardman.github import read_cache
from boardman.settings import settings


async def main() -> None:
    settings.github_read_cache_ttl_seconds = 300.0
    read_cache.clear_read_cache()
    calls = 0

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return json.dumps({"ok": True, "repo": "benchmark/repo"})

    started = time.perf_counter()
    await fetch()
    uncached_seconds = time.perf_counter() - started

    read_cache.clear_read_cache()
    calls = 0
    started = time.perf_counter()
    await read_cache.cached("planning:benchmark/repo", fetch, ok=read_cache.json_ok)
    cold_cached_seconds = time.perf_counter() - started
    started = time.perf_counter()
    await read_cache.cached("planning:benchmark/repo", fetch, ok=read_cache.json_ok)
    warm_cached_seconds = time.perf_counter() - started

    read_cache.clear_read_cache()
    calls = 0
    started = time.perf_counter()
    await asyncio.gather(
        *[
            read_cache.cached("planning:benchmark/repo", fetch, ok=read_cache.json_ok)
            for _ in range(5)
        ]
    )
    concurrent_seconds = time.perf_counter() - started

    print(
        json.dumps(
            {
                "uncached_seconds": round(uncached_seconds, 4),
                "cold_cached_seconds": round(cold_cached_seconds, 4),
                "warm_cached_seconds": round(warm_cached_seconds, 4),
                "concurrent_five_seconds": round(concurrent_seconds, 4),
                "concurrent_fetch_calls": calls,
                "cache_stats": read_cache.cache_stats(),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
