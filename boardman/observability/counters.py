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
from typing import Any

_lock = threading.Lock()
_counts: dict[str, int] = {}
_gauges: dict[str, float] = {}
_series: dict[str, list[float]] = {}

# A run away series would grow without bound in a long-lived process; the benchmark only
# ever needs the recent tail.
_SERIES_CAP = 512


def bump(name: str, n: int = 1) -> None:
    """Add to a counter. Counters only ever go up; the reader diffs snapshots."""
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
