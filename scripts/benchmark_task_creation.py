"""Time the assistant on the questions a QA engineer actually asks, and on real writes.

Every prompt runs against the live service, the live board, and the live model. Task
rows created by a run are tagged and deleted at the end, so the benchmark leaves the
board exactly as it found it (pass --keep to inspect them).

    poetry run python scripts/benchmark_task_creation.py
    poetry run python scripts/benchmark_task_creation.py --only create
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
import uuid
from typing import Any

import httpx

AGENT = "http://localhost:8090/api/v1/agent/chat"
REPO = "Team-Deepiri/deepiri-boardman"
BOARD = "269028"
GROUP = "933385"
TAG = f"bench-{uuid.uuid4().hex[:6]}"

# What a QA engineer genuinely needs before writing tickets: where the risk is, what
# changed, who owns it, who should verify it.
READ_PROMPTS: list[tuple[str, str]] = [
    ("risk", "What are the riskiest parts of this repo for a QA engineer to focus on, and why?"),
    ("regressions", "What regressions should I watch for based on the most recent merged PRs?"),
    ("coverage", "Which parts of this codebase look least covered by tests right now?"),
    (
        "qa-routing",
        "Who should QA a change to the Plaky client, and what makes them the right pick?",
    ),
    ("board-state", "What Plaky tasks are open right now and which ones are waiting on QA?"),
]

WRITE_PROMPTS: list[tuple[str, str]] = [
    (
        "create-1-named",
        f"Create one Plaky task titled '{TAG} verify webhook dedupe under duplicate delivery'. "
        "Assign it to Ali and set Sergio as the QA engineer. Priority high.",
    ),
    (
        "create-5-risk",
        f"Create 5 Plaky tasks for QA coverage of this repo's highest-risk paths. "
        f"Prefix every title with '{TAG}'. Give each one a real assignee and a QA engineer.",
    ),
]


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((p / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


async def ask(
    client: httpx.AsyncClient,
    message: str,
    *,
    session: str | None,
    writes: bool,
) -> tuple[str, str, float]:
    t0 = time.monotonic()
    r = await client.post(
        AGENT,
        json={
            "message": message,
            "session_id": session,
            "repo": REPO,
            "use_tools": True,
            "allow_writes": writes,
            "plaky_board_id": BOARD,
            "plaky_group_id": GROUP,
        },
        timeout=600,
    )
    dt = time.monotonic() - t0
    if r.status_code != 200:
        return f"HTTP {r.status_code}", session or "", dt
    d = r.json()
    return str(d.get("reply") or ""), str(d.get("session_id") or ""), dt


async def board_tasks() -> list[dict[str, Any]]:
    from boardman.plaky.client import PlakyClient

    res = await PlakyClient().get_tasks(board_id=BOARD, status="all")
    return [t for t in (res.get("tasks") or []) if isinstance(t, dict)]


async def read_fields(task_id: str) -> dict[str, Any]:
    from boardman.plaky.client import PlakyClient

    res = await PlakyClient().get_board_item_public(BOARD, task_id)
    item = res.get("item") or {}
    out: dict[str, Any] = {"title": item.get("title") or ""}
    for f in item.get("fields") or []:
        key, val = f.get("key"), f.get("value")
        if key == "status-8":
            out["status"] = str(val)
        elif key == "status-9":
            out["priority"] = str(val)
        elif key == "person-5":
            out["assignee"] = (val or {}).get("assignedUsers") or []
        elif key == "person-6":
            out["qa"] = (val or {}).get("assignedUsers") or []
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["read", "create"], default=None)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--rounds", type=int, default=1)
    args = ap.parse_args()

    rows: list[tuple[str, str, float, str]] = []
    async with httpx.AsyncClient(timeout=600) as client:
        if args.only != "create":
            print("== QA-engineer context questions (read-only, tools on) ==", flush=True)
            session: str | None = None
            for _round in range(args.rounds):
                for name, prompt in READ_PROMPTS:
                    reply, session, dt = await ask(client, prompt, session=session, writes=False)
                    ok = len(reply) > 40 and not reply.startswith("HTTP ")
                    rows.append(("read", name, dt, "ok" if ok else "EMPTY/ERROR"))
                    print(
                        f"  {dt:7.1f}s  {name:<14} {reply[:88].replace(chr(10), ' ')}", flush=True
                    )

        created: list[str] = []
        if args.only != "read":
            print("\n== Task creation (writes on) ==", flush=True)
            before = {str(t.get("id")) for t in await board_tasks()}
            for name, prompt in WRITE_PROMPTS:
                reply, _sid, dt = await ask(client, prompt, session=None, writes=True)
                after = await board_tasks()
                new = [t for t in after if str(t.get("id")) not in before]
                before = {str(t.get("id")) for t in after}
                created.extend(str(t.get("id")) for t in new)
                rows.append(("create", name, dt, f"{len(new)} task(s)"))
                per = f"{dt / len(new):.1f}s/task" if new else "no tasks"
                print(f"  {dt:7.1f}s  {name:<14} created {len(new)} ({per})", flush=True)
                print(f"           {reply[:200].replace(chr(10), ' ')}", flush=True)

            if created:
                print("\n== What landed on the board ==", flush=True)
                filled_assignee = filled_qa = 0
                for tid in created:
                    f = await read_fields(tid)
                    a, q = f.get("assignee") or [], f.get("qa") or []
                    filled_assignee += 1 if a else 0
                    filled_qa += 1 if q else 0
                    print(
                        f"  {tid}  status={f.get('status')} pri={f.get('priority')} "
                        f"assignee={a} qa={q}  {str(f.get('title'))[:52]}",
                        flush=True,
                    )
                print(
                    f"\n  assignee filled: {filled_assignee}/{len(created)}   "
                    f"QA filled: {filled_qa}/{len(created)}",
                    flush=True,
                )

    print("\n" + "=" * 76)
    reads = [d for kind, _n, d, _s in rows if kind == "read"]
    writes = [d for kind, _n, d, _s in rows if kind == "create"]
    if reads:
        print(
            f"READ   n={len(reads)}  p50={_pct(reads, 50):.1f}s  p95={_pct(reads, 95):.1f}s  "
            f"mean={statistics.mean(reads):.1f}s"
        )
    if writes:
        print(f"WRITE  n={len(writes)}  total={sum(writes):.1f}s  slowest={max(writes):.1f}s")
    print("=" * 76)

    if created and not args.keep:
        from boardman.plaky.client import PlakyClient

        p = PlakyClient()
        gone = 0
        for tid in created:
            if (await p.delete_board_item(BOARD, tid)).get("ok"):
                gone += 1
        print(f"cleanup: deleted {gone}/{len(created)} benchmark tasks (tag {TAG})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
