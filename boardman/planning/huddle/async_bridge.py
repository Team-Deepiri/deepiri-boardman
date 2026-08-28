"""Run a coroutine to completion from synchronous code, safely.

The huddle planning providers expose a synchronous public API
(``context_markdown``, ``fetch_recent_items``, ``generate``) but do their I/O
with ``async`` internals. Bridging with a bare ``asyncio.run()`` is fragile:

* ``asyncio.run()`` raises ``RuntimeError: asyncio.run() cannot be called from a
  running event loop`` if the sync method is ever invoked from within a running
  loop (e.g. directly inside an async route or agent coroutine), and
* it creates and tears down a fresh event loop on every call.

``run_sync`` centralizes the bridge so callers do not repeat the pattern: when no
event loop is running on the current thread it uses ``asyncio.run``; when a loop
is already running it offloads the coroutine to a dedicated worker thread with
its own loop, so the sync API is safe to call from any context.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

_T = TypeVar("_T")


def run_sync(coro: Coroutine[object, object, _T]) -> _T:
    """Execute ``coro`` and return its result from synchronous code."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop running on this thread — safe to own one for this call.
        return asyncio.run(coro)

    # A loop is already running here; run the coroutine on a separate thread
    # with its own event loop to avoid clashing with the active loop.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
