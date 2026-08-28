"""Shared HTTP clients: pay the TLS handshake once, not per call.

Every GitHub helper used to open its own ``httpx.AsyncClient``, so every tool call paid
DNS + TCP + TLS to api.github.com and threw the connection away. Measured on this
machine: 1.4-2.8s on a cold client vs ~35ms on a warm one. A deep repo question makes
6-10 GitHub calls — 8-16 seconds of pure handshake per answer. The Plaky client had the
same shape.

One client per event loop (pytest-asyncio runs a fresh loop per test; a client is bound
to the loop that created it). The SSL context is built once per process — building it is
most of the "cold client" cost.

``shared_github_client()`` / ``shared_plaky_client()`` are async context managers that
do NOT close on exit, so existing ``async with ... as client:`` call sites swap in with
a one-line change.
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Any

import httpx

_ssl_ctx: ssl.SSLContext | None = None
_gh_clients: dict[Any, httpx.AsyncClient] = {}
_plaky_clients: dict[Any, httpx.AsyncClient] = {}


def _ssl() -> ssl.SSLContext:
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = httpx.create_ssl_context()
    return _ssl_ctx


def _counting_hook(kind: str):
    """Count every outbound call at the one place they all pass through.

    Instrumenting call sites would mean touching dozens of them and missing the next one
    someone adds. Both pools are created here, so this is the only seam that can't drift.
    """

    async def _hook(_request: httpx.Request) -> None:
        from boardman.observability.counters import bump

        bump(f"{kind}.requests")

    return _hook


def _client_for_loop(pool: dict[Any, httpx.AsyncClient]) -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    c = pool.get(loop)
    if c is None or c.is_closed:
        kind = "github" if pool is _gh_clients else "plaky"
        c = httpx.AsyncClient(
            verify=_ssl(),
            # read=90s is the largest budget any current call site used; shrinking it
            # here would silently truncate the defect scan and planning context.
            timeout=httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=60.0),
            limits=httpx.Limits(max_keepalive_connections=24, max_connections=48),
            follow_redirects=True,
            event_hooks={"request": [_counting_hook(kind)]},
        )
        pool[loop] = c
    return c


class _NonClosing:
    """Adapter so `async with shared_github_client() as client:` reuses the pool."""

    def __init__(self, pool: dict[Any, httpx.AsyncClient]):
        self._pool = pool

    async def __aenter__(self) -> httpx.AsyncClient:
        return _client_for_loop(self._pool)

    async def __aexit__(self, *exc: Any) -> bool:
        return False  # never swallow exceptions, never close the shared client


def shared_github_client() -> _NonClosing:
    return _NonClosing(_gh_clients)


def shared_plaky_client() -> _NonClosing:
    return _NonClosing(_plaky_clients)


def github_http_client() -> httpx.AsyncClient:
    """Direct handle for call sites that do not use a context manager."""
    return _client_for_loop(_gh_clients)


async def aclose_shared_http_clients() -> None:
    """Close this loop's clients — called from the app lifespan shutdown."""
    loop = asyncio.get_running_loop()
    for pool in (_gh_clients, _plaky_clients):
        c = pool.pop(loop, None)
        if c is not None and not c.is_closed:
            await c.aclose()
