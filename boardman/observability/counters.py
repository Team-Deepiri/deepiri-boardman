"""Counters for the things the speed work is judged on.

The acceptance spec asks for LLM call count, external API call count, tool count, context
size and cache hit rate per request — none of which were measurable. Latency alone cannot
tell you WHY an answer got slower, and "it feels faster" is not evidence.

Deliberately boring: a dict of ints behind a lock, read over HTTP by the benchmark, which
diffs two snapshots around one request. Nothing is exported, nothing is sampled, and
nothing here is on a hot loop — a chat turn bumps these a few dozen times at most.
"""

from __future__ import annotations

import threading
from contextvars import ContextVar
from typing import Any

#: True while the current task is background work (a deferred job, a sweep), so its calls
#: are not billed to whatever request happened to be in flight at the time. Without this a
#: stale-while-revalidate refresh — the whole point of which is to be off the request path
#: — shows up as eight GitHub calls "made by" the question that triggered it, and the
#: benchmark reports the opposite of what happened.
_background: ContextVar[bool] = ContextVar("boardman_background_work", default=False)

_lock = threading.Lock()
_counts: dict[str, int] = {}
_gauges: dict[str, float] = {}
_series: dict[str, list[float]] = {}

# A run away series would grow without bound in a long-lived process; the benchmark only
# ever needs the recent tail.
_SERIES_CAP = 512


class background_work:  # noqa: N801 - reads as a verb at the call site
    """Mark everything inside as background, so its cost is not billed to a request.

    async with background_work():
        await refresh_everything()
    """

    def __init__(self) -> None:
        self._token: Any = None

    def __enter__(self) -> None:
        self._token = _background.set(True)

    def __exit__(self, *_exc: Any) -> bool:
        if self._token is not None:
            _background.reset(self._token)
        return False

    async def __aenter__(self) -> None:
        self.__enter__()

    async def __aexit__(self, *exc: Any) -> bool:
        return self.__exit__(*exc)


def in_background() -> bool:
    return bool(_background.get())


def bump(name: str, n: int = 1) -> None:
    """Add to a counter. Counters only ever go up; the reader diffs snapshots.

    Work running under :class:`background_work` is counted under a ``.background`` name so
    a reader can tell "the assistant made this call" from "a refresh happened to be
    running at the same time".
    """
    if _background.get():
        name = f"{name}.background"
    with _lock:
        _counts[name] = _counts.get(name, 0) + n


def set_gauge(name: str, value: float) -> None:
    """Record a last-value measurement (context size of the most recent turn)."""
    with _lock:
        _gauges[name] = float(value)


def observe(name: str, value: float) -> None:
    """Append to a bounded series, for percentiles over many turns."""
    with _lock:
        rows = _series.setdefault(name, [])
        rows.append(float(value))
        if len(rows) > _SERIES_CAP:
            del rows[: len(rows) - _SERIES_CAP]


def cache_hit(cache: str) -> None:
    bump(f"cache.{cache}.hit")


def cache_miss(cache: str) -> None:
    bump(f"cache.{cache}.miss")


def snapshot() -> dict[str, Any]:
    """Everything recorded so far. Cheap enough to call between requests."""
    with _lock:
        return {
            "counts": dict(_counts),
            "gauges": dict(_gauges),
            "series": {k: list(v) for k, v in _series.items()},
        }


def reset() -> None:
    """Only for tests and for a benchmark that wants a clean floor."""
    with _lock:
        _counts.clear()
        _gauges.clear()
        _series.clear()


def cache_hit_rate(snap: dict[str, Any] | None = None) -> dict[str, float]:
    """{cache name: hit rate 0..1} from a snapshot (or the live counters)."""
    counts = (snap or snapshot()).get("counts", {})
    names = {
        k.split(".", 2)[1]
        for k in counts
        if k.startswith("cache.") and k.rsplit(".", 1)[-1] in ("hit", "miss")
    }
    out: dict[str, float] = {}
    for name in sorted(names):
        hits = int(counts.get(f"cache.{name}.hit", 0))
        misses = int(counts.get(f"cache.{name}.miss", 0))
        total = hits + misses
        if total:
            out[name] = round(hits / total, 4)
    return out
