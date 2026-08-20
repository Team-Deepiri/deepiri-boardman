"""Assistant performance baseline: the numbers a change is allowed to move.

Records, per prompt, exactly what the acceptance notes ask for:

  time to first token   (streaming endpoint, so it is the real perceived latency)
  total latency
  cache-hit latency     (the same question asked twice; second run must be fast)
  LLM calls             (counted from the turn's own instrumentation)
  tool / external calls (counted from the recorded tool traces)

Read-only: it asks questions, it never writes to a board.

    poetry run python scripts/benchmark_assistant.py --label before
    poetry run python scripts/benchmark_assistant.py --label after --compare before
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "boardman.db"
RESULTS = ROOT / ".benchmarks"
# Overridable so a baseline build can be benchmarked side by side on another port
# (see --base/--db): the before/after numbers have to come from the same prompts.
BASE = "http://localhost:8090/api/v1"
REPO = "Team-Deepiri/deepiri-boardman"
BOARD, GROUP = "269028", "933385"

# The questions people actually ask, spread across the cost profile.
PROMPTS: list[tuple[str, str]] = [
    ("trivial", "are you online?"),
    ("identity", "what is deepiri boardman"),
    ("cross-repo", "what is deepiri sorge"),
    ("board-state", "what's on the board right now?"),
    ("pr-state", "what pull requests are open?"),
    ("judgment", "what should we work on next?"),
]


def _turn_counts(before_id: int) -> tuple[int, int]:
    """(llm_turns, tool_calls) recorded since ``before_id``."""
    if not DB.exists():
        return (0, 0)
    con = sqlite3.connect(str(DB))
    try:
        rows = con.execute(
            "select tool_calls_json from agent_messages where id > ? and role = 'assistant'",
            (before_id,),
        ).fetchall()
    except sqlite3.Error:
        return (0, 0)
    finally:
        con.close()
    tools = 0
    for (raw,) in rows:
        if not raw:
            continue
        try:
            tools += len(json.loads(raw) or [])
        except (TypeError, ValueError):
            pass
    return (len(rows), tools)


def _max_message_id() -> int:
    if not DB.exists():
        return 0
    con = sqlite3.connect(str(DB))
    try:
        row = con.execute("select coalesce(max(id), 0) from agent_messages").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        con.close()


async def stream_once(client: httpx.AsyncClient, message: str) -> tuple[float, float, int]:
    """(seconds_to_first_token, total_seconds, reply_chars) over the SSE endpoint."""
    payload = {
        "message": message,
        "repo": REPO,
        "use_tools": True,
        "allow_writes": False,
        "plaky_board_id": BOARD,
        "plaky_group_id": GROUP,
    }
    t0 = time.monotonic()
    first: float | None = None
    chars = 0
    async with client.stream("POST", f"{BASE}/agent/chat/stream", json=payload) as r:
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
            elif evt.get("type") in ("done", "error"):
                break
    total = time.monotonic() - t0
    return (first if first is not None else total, total, chars)


async def run(label: str) -> dict[str, Any]:
    out: dict[str, Any] = {"label": label, "rows": []}
    async with httpx.AsyncClient(timeout=600) as client:
        for name, prompt in PROMPTS:
            before = _max_message_id()
            ttft, total, chars = await stream_once(client, prompt)
            llm, tools = _turn_counts(before)
            # Same question again: exercises the warm caches (schema, roster, repo context).
            _ttft2, warm, _c2 = await stream_once(client, prompt)
            row = {
                "name": name,
                "ttft_s": round(ttft, 2),
                "total_s": round(total, 2),
                "cache_hit_s": round(warm, 2),
                "llm_turns": llm,
                "tool_calls": tools,
                "chars": chars,
            }
            out["rows"].append(row)
            print(
                f"  {name:<12} ttft={row['ttft_s']:>6.2f}s total={row['total_s']:>6.2f}s "
                f"warm={row['cache_hit_s']:>6.2f}s llm={llm} tools={tools} chars={chars}",
                flush=True,
            )
    totals = [r["total_s"] for r in out["rows"]]
    ttfts = [r["ttft_s"] for r in out["rows"]]
    warms = [r["cache_hit_s"] for r in out["rows"]]
    out["summary"] = {
        "ttft_mean": round(statistics.mean(ttfts), 2),
        "total_mean": round(statistics.mean(totals), 2),
        "total_max": round(max(totals), 2),
        "cache_hit_mean": round(statistics.mean(warms), 2),
        "tool_calls": sum(r["tool_calls"] for r in out["rows"]),
        "llm_turns": sum(r["llm_turns"] for r in out["rows"]),
    }
    return out


def compare(now: dict[str, Any], prev: dict[str, Any]) -> int:
    print("\n" + "=" * 78)
    print(f"COMPARISON  {prev['label']} -> {now['label']}")
    print("=" * 78)
    a, b = prev["summary"], now["summary"]
    worse = 0
    for key, budget in (
        ("ttft_mean", 1.25),
        ("total_mean", 1.25),
        ("cache_hit_mean", 1.25),
    ):
        old, new = a.get(key, 0), b.get(key, 0)
        delta = (new - old) / old * 100 if old else 0.0
        flag = ""
        if old and new > old * budget:
            flag = "  <-- REGRESSION"
            worse += 1
        print(f"  {key:<16} {old:>7.2f}s -> {new:>7.2f}s  ({delta:+.0f}%){flag}")
    for key in ("tool_calls", "llm_turns"):
        print(f"  {key:<16} {a.get(key):>7} -> {b.get(key):>7}")
    print("=" * 78)
    return 1 if worse else 0


async def main() -> int:
    global BASE, DB

    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--compare", default="")
    ap.add_argument("--base", default=BASE, help="API root of the instance under test")
    ap.add_argument("--db", default=str(DB), help="sqlite file that instance writes to")
    args = ap.parse_args()

    BASE = args.base.rstrip("/")
    DB = Path(args.db)

    RESULTS.mkdir(exist_ok=True)
    print(f"== assistant benchmark [{args.label}] ==", flush=True)
    now = await run(args.label)
    (RESULTS / f"{args.label}.json").write_text(json.dumps(now, indent=2), encoding="utf-8")

    s = now["summary"]
    print(
        f"\nttft_mean={s['ttft_mean']}s total_mean={s['total_mean']}s "
        f"cache_hit_mean={s['cache_hit_mean']}s tools={s['tool_calls']} llm={s['llm_turns']}"
    )
    if args.compare:
        prior = RESULTS / f"{args.compare}.json"
        if prior.exists():
            return compare(now, json.loads(prior.read_text(encoding="utf-8")))
        print(f"(no baseline named {args.compare!r} to compare against)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
