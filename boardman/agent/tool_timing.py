"""Per-tool latency logging for the assistant.

A slow answer is either slow tools or slow model turns, and without timings the two are
indistinguishable — every tuning decision becomes a guess. Wrapping the tools costs one
`time.monotonic()` per call and turns "it took two minutes" into a list you can act on.

Emitted at INFO as one line per call:

    agent tool github_repo_planning_context took 4.81s ok
    agent tool github_search_code took 12.44s ok
    agent tool plaky_create_task took 0.93s ERROR ValueError

and one summary per turn (see ``turn_timing``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)

# Per-turn accumulator: tool name -> [total seconds, call count]. A ContextVar keeps
# concurrent chat turns from mixing their numbers together.
_turn_totals: ContextVar[dict[str, list[float]] | None] = ContextVar("turn_totals", default=None)
# Per-turn (tool, args-repr) call counts: the loop breaker (latency plan step 4) refuses
# the third identical call in one turn instead of letting the model spin to the
# recursion ceiling repeating a fetch that will not change.
_turn_calls: ContextVar[dict[tuple[str, str], int] | None] = ContextVar("turn_calls", default=None)

# Rolling end-to-end turn latencies for p50/p95 (latency plan step 1). Process-local.
_TURN_LATENCIES: list[float] = []


def record_turn_latency(seconds: float) -> None:
    _TURN_LATENCIES.append(seconds)
    if len(_TURN_LATENCIES) > 200:
        del _TURN_LATENCIES[: len(_TURN_LATENCIES) - 200]


def latency_percentiles() -> dict[str, float]:
    if not _TURN_LATENCIES:
        return {"count": 0, "p50": 0.0, "p95": 0.0}
    xs = sorted(_TURN_LATENCIES)
    return {
        "count": len(xs),
        "p50": round(xs[len(xs) // 2], 2),
        "p95": round(xs[min(len(xs) - 1, int(len(xs) * 0.95))], 2),
    }


def start_turn() -> None:
    _turn_totals.set({})
    _turn_calls.set({})


def turn_timing() -> str:
    """Compact summary of this turn's tool time, slowest first."""
    totals = _turn_totals.get()
    if not totals:
        return "no tool calls"
    rows = sorted(totals.items(), key=lambda kv: -kv[1][0])
    total = sum(v[0] for v in totals.values())
    calls = int(sum(v[1] for v in totals.values()))
    detail = ", ".join(f"{name} {secs:.1f}s x{int(n)}" for name, (secs, n) in rows)
    return f"{total:.1f}s in {calls} tool call(s): {detail}"


def _record(name: str, elapsed: float) -> None:
    totals = _turn_totals.get()
    if totals is None:
        return
    row = totals.setdefault(name, [0.0, 0.0])
    row[0] += elapsed
    row[1] += 1


def _timed(name: str, coro_fn: Callable[..., Any]) -> Callable[..., Any]:
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls = _turn_calls.get()
        if calls is not None:
            key = (name, repr(args) + "|" + repr(sorted(kwargs.items())))
            n = calls.get(key, 0) + 1
            calls[key] = n
            if n > 2:
                logger.warning("agent tool %s: identical call #%d refused (loop breaker)", name, n)
                return (
                    "LOOP DETECTED: you already called this tool with these exact arguments "
                    f"{n - 1} times this turn and the result will not change. Use the result "
                    "you already have, or change the arguments. Do not call it again."
                )
        started = time.monotonic()
        outcome = "ok"
        try:
            return await coro_fn(*args, **kwargs)
        except Exception as e:
            outcome = f"ERROR {type(e).__name__}"
            raise
        finally:
            elapsed = time.monotonic() - started
            _record(name, elapsed)
            logger.info("agent tool %s took %.2fs %s", name, elapsed, outcome)

    return wrapper


def with_timing(tools: list[StructuredTool]) -> list[StructuredTool]:
    """Return the same tools, each wrapped so its wall time is logged.

    Rebuilt rather than mutated: a StructuredTool's coroutine is shared with the factory
    that produced it, and mutating in place would double-wrap on the next turn.
    """
    out: list[StructuredTool] = []
    for t in tools:
        fn = getattr(t, "coroutine", None)
        if fn is None:
            out.append(t)
            continue
        out.append(
            StructuredTool.from_function(
                coroutine=_timed(t.name, fn),
                name=t.name,
                description=t.description,
                args_schema=getattr(t, "args_schema", None),
            )
        )
    return out
