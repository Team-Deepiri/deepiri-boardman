"""The benchmark the Brain work is judged against.

Section 19 of the spec asks for a scenario matrix, p50/p95 rather than one lucky request,
time-to-first-token separately from total latency, and the counts that explain a number:
LLM calls, external API calls, tool calls, context size, cache hit rate. All of those come
from /api/v1/metrics, diffed around each request, so a latency change can always be traced
to a call that was or was not made.

    poetry run python scripts/benchmark_brain.py --label before
    poetry run python scripts/benchmark_brain.py --label after --compare before

    # the cold-cache scenario needs a freshly started process
    poetry run python scripts/benchmark_brain.py --only cold_cache --label cold-after

Read-only by default. --with-writes adds the task-creation scenario, which creates real
Plaky tasks on the deepiri-boardman group and deletes them again at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / ".benchmarks"
REPO = "Team-Deepiri/deepiri-boardman"
BOARD, GROUP = "269028", "933385"

# Scenario -> (prompt, needs_writes). Names match the spec's table.
SCENARIOS: dict[str, tuple[str, bool]] = {
    "cached_fact": ("what is boardman?", False),
    "repo_briefing": ("what is deepiri-axiom?", False),
    "live_state": ("what pull requests are open on boardman right now?", False),
    "five_task_planning": ("what are the five most important things boardman needs?", False),
    "deep_code": ("how does boardman decide which QA engineer to assign?", False),
    "audit_behavior": (
        "is the GitHub to Plaky lifecycle actually implemented correctly?",
        False,
    ),
    "task_creation": (
        "create one task for boardman: add a retry budget to the plaky client. "
        "prefix the title with [bench] so it is easy to find.",
        True,
    ),
}
COLD = "cold_cache"
CONCURRENT = "concurrent_same_query"


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return round(values[0], 2)
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 2)


class Meter:
    """Reads /metrics and diffs two snapshots around one request."""

    def __init__(self, base: str, client: httpx.AsyncClient) -> None:
        self.base, self.client = base, client

    async def read(self) -> dict[str, Any]:
        try:
            r = await self.client.get(f"{self.base}/metrics", timeout=15)
            return r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, OSError, ValueError) as e:
            print(f"  ! /metrics unreadable ({type(e).__name__}: {e}); counters will read 0")
            return {}

    @staticmethod
    def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        b, a = before.get("counts") or {}, after.get("counts") or {}
        delta = {k: int(a.get(k, 0)) - int(b.get(k, 0)) for k in set(a) | set(b)}
        delta = {k: v for k, v in delta.items() if v}
        hits = sum(v for k, v in delta.items() if k.startswith("cache.") and k.endswith(".hit"))
        misses = sum(v for k, v in delta.items() if k.startswith("cache.") and k.endswith(".miss"))
        # ".background" counters are deferred jobs that happened to overlap the request.
        # Charging them to it reports the opposite of what happened: a refresh whose whole
        # purpose is to be off the request path would look like eight calls the question
        # made. Reported, never conflated.
        return {
            "llm_calls": delta.get("llm.calls", 0),
            "tool_calls": delta.get("tool.calls", 0),
            "github_requests": delta.get("github.requests", 0),
            "plaky_requests": delta.get("plaky.requests", 0),
            "background_requests": (
                delta.get("github.requests.background", 0)
                + delta.get("plaky.requests.background", 0)
            ),
            "cache_hits": hits,
            "cache_misses": misses,
            "cache_hit_rate": round(hits / (hits + misses), 3) if (hits + misses) else None,
            "context_chars": int((after.get("gauges") or {}).get("llm.last_prompt_chars", 0)),
        }


async def ask_stream(
    client: httpx.AsyncClient, base: str, message: str, writes: bool
) -> tuple[float, float, int, str]:
    """(ttft, total, chars, error) over the SSE endpoint.

    A transport error returns rather than raises: one slow scenario must not throw away
    the whole matrix, and a run that lost a scenario has to say so instead of silently
    reporting the rest as if it were complete.
    """
    payload = {
        "message": message,
        "repo": REPO,
        "use_tools": True,
        "allow_writes": writes,
        "plaky_board_id": BOARD,
        "plaky_group_id": GROUP,
    }
    t0 = time.monotonic()
    first: float | None = None
    chars = 0
    server_err = ""
    try:
        async with client.stream("POST", f"{base}/agent/chat/stream", json=payload) as r:
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    evt = json.loads(line[5:].strip() or "{}")
                except ValueError:
                    continue
                if evt.get("type") == "token":
                    if first is None:
                        first = time.monotonic() - t0
                    chars += len(str(evt.get("text") or evt.get("token") or ""))
                elif evt.get("type") == "error":
                    # A provider outage answers in 0.3s. Counted as a success it would
                    # read as the fastest result the benchmark has ever produced.
                    server_err = f"server error: {str(evt.get('message') or 'unknown')[:120]}"
                    break
                elif evt.get("type") == "done":
                    break
    except Exception as e:  # noqa: BLE001 - the failure IS the measurement, reported below
        # A run that died is not a fast run: the timings come back with the error attached
        # so `compare` can never read an outage as the best result ever recorded.
        total = time.monotonic() - t0
        return (first if first is not None else total, total, chars, f"{type(e).__name__}: {e}")
    total = time.monotonic() - t0
    return (first if first is not None else total, total, chars, server_err)


async def run_scenario(
    client: httpx.AsyncClient, base: str, name: str, prompt: str, writes: bool, repeats: int
) -> dict[str, Any]:
    meter = Meter(base, client)
    ttfts: list[float] = []
    totals: list[float] = []
    warm: list[float] = []
    first_call: dict[str, Any] = {}
    chars = 0

    errors: list[str] = []
    counted_run = 0
    for _i in range(repeats):
        before = await meter.read()
        ttft, total, c, err = await ask_stream(client, base, prompt, writes)
        after = await meter.read()
        d = Meter.diff(before, after)
        if err:
            errors.append(err[:120])
            continue
        # Counters come from the FIRST successful run and latency from all of them, so
        # the row says which run its counts describe rather than implying they are a
        # property of the median.
        if not first_call:
            first_call, chars = d, c
            counted_run = len(totals) + 1
        else:
            # Later repeats of the SAME question are the cache-hit measurement.
            warm.append(total)
        ttfts.append(ttft)
        totals.append(total)

    row = {
        "scenario": name,
        "ttft_p50": pct(ttfts, 0.5),
        "ttft_p95": pct(ttfts, 0.95),
        "total_p50": pct(totals, 0.5),
        "total_p95": pct(totals, 0.95),
        "warm_p50": pct(warm, 0.5) if warm else None,
        "reply_chars": chars,
        "runs_ok": len(totals),
        "counts_from_run": counted_run,
        "errors": errors,
        **first_call,
    }
    if errors:
        print(f"  {name:<20} {len(errors)} of {repeats} runs FAILED: {errors[0]}", flush=True)
    print(
        f"  {name:<20} ttft={row['ttft_p50']:>6.2f}s total={row['total_p50']:>6.2f}s "
        f"warm={row['warm_p50'] if row['warm_p50'] is not None else '-':>6} "
        f"llm={row.get('llm_calls')} tools={row.get('tool_calls')} "
        f"gh={row.get('github_requests')} plaky={row.get('plaky_requests')} "
        f"bg={row.get('background_requests')} "
        f"ctx={row.get('context_chars')} hit={row.get('cache_hit_rate')}",
        flush=True,
    )
    return row


async def run_concurrent(client: httpx.AsyncClient, base: str, n: int = 4) -> dict[str, Any]:
    """Same question from N callers at once. Coalescing shows up as external calls well
    below N times the single-request cost."""
    meter = Meter(base, client)
    prompt = "what is deepiri-axiom?"
    # Warm first so we measure coalescing, not a cold miss.
    await ask_stream(client, base, prompt, False)
    before = await meter.read()
    t0 = time.monotonic()
    results = await asyncio.gather(
        *(ask_stream(client, base, prompt, False) for _ in range(n)), return_exceptions=True
    )
    wall = time.monotonic() - t0
    after = await meter.read()
    d = Meter.diff(before, after)
    ok = [r for r in results if isinstance(r, tuple) and not r[3]]
    row = {
        "scenario": CONCURRENT,
        "callers": n,
        "wall_s": round(wall, 2),
        "slowest_s": round(max((r[1] for r in ok), default=0.0), 2),
        "failed": len(results) - len(ok),
        **d,
    }
    print(
        f"  {CONCURRENT:<20} wall={row['wall_s']}s slowest={row['slowest_s']}s "
        f"gh={row['github_requests']} plaky={row['plaky_requests']} llm={row['llm_calls']} "
        f"(for {n} identical callers)",
        flush=True,
    )
    return row


async def run_cold(client: httpx.AsyncClient, base: str) -> dict[str, Any]:
    """First question against a freshly started process. Restart the server before this."""
    meter = Meter(base, client)
    before = await meter.read()
    ttft, total, chars, err = await ask_stream(client, base, "what is boardman?", False)
    after = await meter.read()
    row = {
        "scenario": COLD,
        "ttft_p50": round(ttft, 2),
        "total_p50": round(total, 2),
        "reply_chars": chars,
        "errors": [err] if err else [],
        **Meter.diff(before, after),
    }
    print(
        f"  {COLD:<20} ttft={row['ttft_p50']:>6.2f}s total={row['total_p50']:>6.2f}s "
        f"gh={row.get('github_requests')} plaky={row.get('plaky_requests')}",
        flush=True,
    )
    return row


async def cleanup_bench_tasks() -> int:
    """Delete anything the write scenario created. Titles are prefixed [bench]."""
    try:
        from boardman.plaky.client import PlakyClient
    except Exception as e:  # noqa: BLE001 - importing settings validates the env too
        # Not just ImportError: pydantic-settings raises ValidationError at import time
        # when an env var is malformed, and giving up there would leave [bench] tasks
        # sitting on the live board.
        print(f"  ! cannot clean up bench tasks: {type(e).__name__}: {e}", flush=True)
        return 0
    c = PlakyClient()
    try:
        listing = await c.get_tasks(board_id=BOARD, status="all")
    except Exception as e:  # noqa: BLE001 - cleanup runs before the results are written
        # Nothing here may escape: this is called ahead of the JSON write, so a failure
        # to list the board would throw away the run's measurements as well as the tasks.
        print(
            f"  ! cannot list board {BOARD} to clean up ({type(e).__name__}: {e}); "
            "leaving bench tasks",
            flush=True,
        )
        return 0
    removed = 0
    for t in listing.get("tasks") or []:
        title = str(t.get("name") or t.get("title") or "")
        if "[bench]" in title.casefold():
            try:
                await c.delete_board_item(BOARD, str(t.get("id")))
                removed += 1
            except Exception as e:  # noqa: BLE001 - cleanup is best effort
                print(f"  ! could not delete {t.get('id')}: {type(e).__name__}: {e}", flush=True)
    return removed


def compare(now: dict[str, Any], prev: dict[str, Any]) -> int:
    print("\n" + "=" * 100)
    print(f"COMPARISON  {prev.get('label')} -> {now.get('label')}")
    print("=" * 100)
    prior = {r["scenario"]: r for r in prev.get("rows", [])}
    regressions = 0
    header = f"  {'scenario':<20} {'total p50':>20} {'llm':>10} {'gh+plaky':>12} {'context':>12}"
    print(header)
    for row in now.get("rows", []):
        name = row["scenario"]
        old = prior.get(name)
        if not old:
            print(f"  {name:<20} {'(new)':>20}")
            continue

        def fmt(key: str, o: dict, n: dict) -> str:
            a, b = o.get(key), n.get(key)
            if a is None or b is None:
                return f"{b}"
            return f"{a} -> {b}"

        old_total = old.get("total_p50") or 0
        new_total = row.get("total_p50") or 0
        flag = ""
        if not row.get("runs_ok", 1):
            # Every run failed. Its 0.0s must never read as an improvement, and it must
            # not pass the gate in silence — a total outage would otherwise be the best
            # result this benchmark has ever recorded.
            flag = f"  <-- ALL {len(row.get('errors') or [])} RUNS FAILED"
            regressions += 1
        # Same 25% budget the older benchmark used, and only for meaningful durations.
        elif old_total > 1.0 and new_total > old_total * 1.25:
            flag = "  <-- REGRESSION"
            regressions += 1
        api_old = (old.get("github_requests") or 0) + (old.get("plaky_requests") or 0)
        api_new = (row.get("github_requests") or 0) + (row.get("plaky_requests") or 0)
        print(
            f"  {name:<20} {f'{old_total} -> {new_total}s':>20} "
            f"{fmt('llm_calls', old, row):>10} {f'{api_old} -> {api_new}':>12} "
            f"{fmt('context_chars', old, row):>12}{flag}"
        )
    print("=" * 100)
    return 1 if regressions else 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8090/api/v1")
    ap.add_argument("--label", default="run")
    ap.add_argument("--compare", default="")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--only", default="", help="comma-separated scenario names")
    ap.add_argument("--with-writes", action="store_true")
    ap.add_argument("--skip-concurrent", action="store_true")
    ap.add_argument("--no-warmup", action="store_true", help="measure the first call too")
    ap.add_argument(
        "--settle", type=float, default=6.0, help="seconds to let background work finish"
    )
    args = ap.parse_args()

    base = args.base.rstrip("/")
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    RESULTS.mkdir(exist_ok=True)
    rows: list[dict[str, Any]] = []

    print(f"== brain benchmark [{args.label}] against {base} ==", flush=True)
    async with httpx.AsyncClient(timeout=600) as client:
        if not only or COLD in only:
            if only:  # cold only makes sense as the very first request after a restart
                rows.append(await run_cold(client, base))
        wanted = [
            (n, p, w)
            for n, (p, w) in SCENARIOS.items()
            if (not only or n in only) and (not w or args.with_writes)
        ]
        if wanted and not args.no_warmup:
            # One pass first so the numbers describe steady state rather than one-time
            # repairs. A stale snapshot triggers a background refresh; that refresh is real
            # work and it is reported, but it is not what the tenth caller of the day pays.
            print("  (warm-up pass; not measured)", flush=True)
            for _name, prompt, writes in wanted:
                await ask_stream(client, base, prompt, writes)
            await asyncio.sleep(args.settle)
        for name, prompt, writes in wanted:
            rows.append(await run_scenario(client, base, name, prompt, writes, args.repeats))
        if not args.skip_concurrent and (not only or CONCURRENT in only):
            rows.append(await run_concurrent(client, base))

    if args.with_writes:
        removed = await cleanup_bench_tasks()
        print(f"\ncleaned up {removed} [bench] task(s)", flush=True)

    out = {"label": args.label, "base": base, "repeats": args.repeats, "rows": rows}
    (RESULTS / f"{args.label}.json").write_text(json.dumps(out, indent=2), encoding="utf-8")

    failed = [r for r in rows if r.get("errors") and not r.get("runs_ok")]
    if failed:
        print("")
        print("SCENARIOS THAT NEVER COMPLETED (their numbers mean nothing):", flush=True)
        for r in failed:
            print(f"  {r['scenario']}: {(r.get('errors') or ['?'])[0]}", flush=True)
    read_rows = [r for r in rows if r.get("total_p50") and r.get("runs_ok")]
    if read_rows:
        print(
            f"\nmedian total across scenarios: "
            f"{round(statistics.median([r['total_p50'] for r in read_rows]), 2)}s"
        )
    if args.compare:
        prior = RESULTS / f"{args.compare}.json"
        if prior.exists():
            return compare(out, json.loads(prior.read_text(encoding="utf-8")))
        print(f"(no baseline named {args.compare!r})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
