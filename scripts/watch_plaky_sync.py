"""Watch the Plaky boards the poller feeds, and report what actually changed.

Turning the poller on for a repo is a claim: that GitHub activity will show up on that
repo's board, correctly, without anyone touching it. This checks the claim instead of
asserting it. Every cycle it snapshots the watched groups, diffs against the previous
snapshot, and prints only what moved — a new task, a status change, a person appearing or
leaving. Nothing here writes to Plaky or GitHub.

    poetry run python scripts/watch_plaky_sync.py --minutes 30
    poetry run python scripts/watch_plaky_sync.py --minutes 5 --interval 20

Pair it with the poller running (TESTING_LIVE_PLAKY=true). The poller replays GitHub
events through the same handlers a production webhook uses, so what shows up here is what
production would do.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# Board -> group the poller's repos route to, resolved live at startup so this never
# drifts from repos.yml or the Plaky catalog.
WATCHED_REPOS_ENV = "TESTING_LIVE_PLAKY_REPOS"

# Last-resort fallback ONLY. The real option names are read from each watched board at
# startup (see option_names_for_board), so a status the team adds or renames shows up here
# without editing this file. This copy is what the script falls back to when the schema
# call fails, so a network blip degrades to slightly stale labels instead of raw ids.
FALLBACK_OPTION_NAMES: dict[str, dict[str, str]] = {
    "Status": {
        "0": "NEEDS ASSIGNED",
        "8": "Assigned",
        "2": "In Progress",
        "3": "Paused",
        "4": "Needs QA",
        "11": "Needs QA Again",
        "5": "In QA",
        "6": "QA Verified",
        "7": "QA Rejected",
        "1": "Completed",
        "9": "Deployed",
        "10": "Continuous",
    },
    "Type": {
        "0": "Story",
        "9": "Task",
        "10": "Bug",
        "12": "Research",
        "17": "Feature",
        "18": "Refactor",
    },
    "Priority": {"0": "VERY IMPORTANT", "1": "High", "2": "Medium", "3": "Low"},
}


async def option_names_for_board(board_id: str) -> dict[str, dict[str, str]]:
    """{field title: {option id: label}} read live from the board's own schema.

    Hardcoding these made the script brittle: a status added or renamed in Plaky printed
    as a bare id, and nobody noticed until a diff looked wrong. The schema bundle already
    normalizes options across Plaky's API shapes, so ask it instead of guessing.
    """
    from boardman.plaky.board_schema import fetch_board_schema_bundle

    try:
        bundle = await fetch_board_schema_bundle(board_id)
    except Exception as e:  # noqa: BLE001 - a watcher must never die on a schema read
        print(
            f"  ! schema for board {board_id} unavailable ({e}); using fallback labels", flush=True
        )
        # Copy per column, like the success path below: the caller must never be handed
        # the module constant itself.
        return {k: dict(v) for k, v in FALLBACK_OPTION_NAMES.items()}

    normalized = bundle.get("normalized") if isinstance(bundle, dict) else None
    # Start from the hardcoded map and OVERLAY the live labels, per column. Replacing a
    # column outright loses any id the schema described without a usable id/name pair, and
    # those ids would then print raw where the old table used to name them.
    out: dict[str, dict[str, str]] = {k: dict(v) for k, v in FALLBACK_OPTION_NAMES.items()}
    described = 0
    for field in (normalized or {}).get("fields") or []:
        if not isinstance(field, dict):
            continue
        title = str(field.get("name") or "").strip()
        options = field.get("options")
        if not title or not isinstance(options, list):
            continue
        labels = {
            str(o.get("id")): str(o.get("name") or "").strip()
            for o in options
            if isinstance(o, dict) and o.get("id") is not None and str(o.get("name") or "").strip()
        }
        if labels:
            described += 1
            out.setdefault(title, {}).update(labels)
    if not described:
        print(
            f"  ! board {board_id} returned no option labels; using fallback labels",
            flush=True,
        )
    return out


async def watched_placements() -> list[tuple[str, str, str]]:
    """[(repo, board_id, group_id)] for everything the poller is configured to watch."""
    from boardman.repos_config import get_routing_async
    from boardman.services.github_poller import resolve_poller_repos
    from boardman.settings import settings

    # resolve_poller_repos, not poller_repos: the latter is empty under the `all`
    # sentinel, so this script would report "nothing to watch" on exactly the
    # configuration the poller is running.
    repos, excluded = await resolve_poller_repos()
    for repo, why in excluded:
        print(f"  - {repo} excluded: {why}", flush=True)

    out: list[tuple[str, str, str]] = []
    for full in repos:
        short = full.rsplit("/", 1)[-1]
        routing = await get_routing_async(full, short, settings.github_org)
        board = str(getattr(routing, "plaky_board_id", "") or "").strip()
        group = str(getattr(routing, "plaky_group_id", "") or "").strip()
        if board:
            out.append((full, board, group))
        else:
            print(f"  ! {full} has no resolvable board; it will sync nowhere", flush=True)
    return out


def _people(value: Any) -> list[str]:
    users = value.get("assignedUsers") if isinstance(value, dict) else None
    return sorted(str(u) for u in (users or []))


async def snapshot(
    board: str,
    group: str,
    names: dict[str, str],
    options: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """{task_id: fields} for one group, as the board currently reads.

    `options` is that board's live {field title: {option id: label}} map; omit it and the
    hardcoded fallback is used.
    """
    from boardman.plaky.client import PlakyClient

    option_names = options if options is not None else FALLBACK_OPTION_NAMES
    client = PlakyClient()
    try:
        listing = await client.list_board_items(board, max_pages=3)
    except Exception as e:  # noqa: BLE001 - one unreadable board must not stop the watch
        print(f"  ! could not read board {board}: {e}", flush=True)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in listing.get("items") or []:
        if not isinstance(item, dict):
            continue
        if group and str(item.get("groupId") or item.get("group_id") or "") not in ("", group):
            continue
        row: dict[str, Any] = {"title": str(item.get("name") or item.get("title") or "")[:90]}
        for f in item.get("fields") or []:
            if not isinstance(f, dict):
                continue
            title, value = str(f.get("title") or ""), f.get("value")
            if f.get("type") == "STATUS" and title in option_names:
                row[title] = option_names[title].get(str(value), str(value))
            elif f.get("type") == "PERSON":
                ids = _people(value)
                row[title] = [names.get(i, i) for i in ids]
        out[str(item.get("id"))] = row
    return out


def diff(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> list[str]:
    """Only what moved. A cycle where nothing changed prints nothing."""
    lines: list[str] = []
    for task_id, row in after.items():
        if task_id not in before:
            lines.append(
                f"  + NEW  {row.get('title')} [{task_id}] "
                f"status={row.get('Status')} type={row.get('Type')} "
                f"priority={row.get('Priority')}"
            )
            continue
        was = before[task_id]
        for field in ("Status", "Type", "Priority", "Assignee", "QA Engineer Assigned"):
            old, new = was.get(field), row.get(field)
            if old != new:
                lines.append(f"  ~ {row.get('title')} [{task_id}] {field}: {old!r} -> {new!r}")
    for task_id, row in before.items():
        if task_id not in after:
            lines.append(f"  - GONE {row.get('title')} [{task_id}]")
    return lines


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--out", default="", help="write the observed changes to this JSON file")
    args = ap.parse_args()

    from boardman.agent.tools.plaky_tools import _person_names

    names = _person_names()
    places = await watched_placements()
    if not places:
        print("nothing to watch: TESTING_LIVE_PLAKY_REPOS resolves to no board", flush=True)
        return 1

    print(f"watching {len(places)} repo(s) for {args.minutes:.0f} min:", flush=True)
    for repo, board, group in places:
        print(f"  {repo:<34} board {board} group {group or '(whole board)'}", flush=True)

    # One schema read per DISTINCT board (several repos often share one), reused for
    # every cycle below. Reading per repo meant duplicate calls and duplicate warnings.
    options: dict[str, dict[str, dict[str, str]]] = {}
    for _repo, b, _g in places:
        if b not in options:
            options[b] = await option_names_for_board(b)
    state = {(b, g): await snapshot(b, g, names, options.get(b)) for _repo, b, g in places}
    for (b, g), rows in state.items():
        print(f"  baseline: board {b} group {g or '*'} has {len(rows)} tasks", flush=True)

    observed: list[dict[str, Any]] = []
    deadline = time.monotonic() + args.minutes * 60
    cycle = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(max(10.0, args.interval))
        cycle += 1
        for repo, b, g in places:
            fresh = await snapshot(b, g, names, options.get(b))
            if not fresh:
                continue
            changes = diff(state[(b, g)], fresh)
            if changes:
                stamp = time.strftime("%H:%M:%S")
                print(f"\n[{stamp}] {repo}", flush=True)
                for line in changes:
                    print(line, flush=True)
                observed.append({"repo": repo, "at": stamp, "changes": changes})
            state[(b, g)] = fresh

    print(f"\n{len(observed)} change event(s) observed over {cycle} cycles", flush=True)
    if not observed:
        print(
            "No board changes. That is a real result when the watched repos were quiet: "
            "the poller only applies activity that happens WHILE it runs.",
            flush=True,
        )
    if args.out:
        Path(args.out).write_text(json.dumps(observed, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
