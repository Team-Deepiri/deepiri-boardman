#!/usr/bin/env python3
"""
Smoke-test Boardman + Plaky + agent using HTTP (httpx).

1. GET /plaky/boards/match?query=... — list boards via API key, rank by name
2. GET groups for chosen board; optional /groups/match?query=...
3. POST /agent/chat with allow_writes + ids

Usage:
  export BOARDMAN_URL=http://127.0.0.1:8090
  export PLAKY_BOARD_QUERY='Deepiri Main'   # phrase to match board name
  # optional: export PLAKY_GROUP_QUERY=Backlog
  # or:      export PLAKY_GROUP_ID=<id>
  poetry run python scripts/test_plaky_agent_http.py

Requires Boardman running with PLAKY_API_KEY and a working LLM (e.g. Ollama).
"""
from __future__ import annotations

import json
import os
import sys

import httpx


def main() -> int:
    base = os.environ.get("BOARDMAN_URL", "http://127.0.0.1:8090").rstrip("/")
    board_query = os.environ.get("PLAKY_BOARD_QUERY", "").strip()
    if not board_query:
        print("Set PLAKY_BOARD_QUERY to a substring of your Plaky board name.", file=sys.stderr)
        return 1

    group_query = os.environ.get("PLAKY_GROUP_QUERY", "").strip()
    group_override = os.environ.get("PLAKY_GROUP_ID", "").strip()

    with httpx.Client(timeout=120.0) as client:
        r = client.get(f"{base}/api/v1/plaky/boards/match", params={"query": board_query})
        r.raise_for_status()
        disc = r.json()
        print("boards/match:", json.dumps(disc, indent=2)[:4000])
        if not disc.get("ok"):
            print("list_boards failed.", file=sys.stderr)
            return 1

        best = disc.get("best")
        if best and best.get("id"):
            board_id = str(best["id"])
            print(f"Using best board match: {best.get('name')!r} id={board_id}")
        else:
            matches = disc.get("matches") or []
            strong = [m for m in matches if m.get("score", 0) >= 100]
            if not strong:
                print(
                    "No confident board match. Raise PLAKY_BOARD_QUERY specificity or pick from `matches`.",
                    file=sys.stderr,
                )
                return 1
            board_id = str(strong[0]["id"])
            print(f"Using top scored board: {strong[0].get('name')!r} id={board_id}")

        group_id = group_override
        if not group_id:
            if group_query:
                gr = client.get(
                    f"{base}/api/v1/plaky/boards/{board_id}/groups/match",
                    params={"query": group_query},
                )
                gr.raise_for_status()
                gd = gr.json()
                print("groups/match:", json.dumps(gd, indent=2)[:4000])
                gb = gd.get("best")
                if gb and gb.get("id"):
                    group_id = str(gb["id"])
                    print(f"Using best group: {gb.get('name')!r} id={group_id}")
            if not group_id:
                gr2 = client.get(f"{base}/api/v1/plaky/boards/{board_id}/groups")
                gr2.raise_for_status()
                gd2 = gr2.json()
                groups = gd2.get("groups") or []
                if groups:
                    group_id = str(groups[0]["id"])
                    print(f"Using first group: {groups[0].get('name')!r} id={group_id}")

        if not group_id:
            print(
                "No group id: set PLAKY_GROUP_QUERY / PLAKY_GROUP_ID or fix group listing.",
                file=sys.stderr,
            )
            return 1

        payload = {
            "message": (
                "Use the plaky_create_task tool exactly once. "
                "Title: Boardman HTTP smoke test. "
                "Description: Created by scripts/test_plaky_agent_http.py — safe to delete. "
                "Priority: low."
            ),
            "allow_writes": True,
            "plaky_board_id": board_id,
            "plaky_group_id": group_id,
        }
        print("POST /api/v1/agent/chat ...")
        ar = client.post(f"{base}/api/v1/agent/chat", json=payload)
        ar.raise_for_status()
        body = ar.json()
        print("agent/chat:", json.dumps(body, indent=2)[:8000])
        if not body.get("ok", True):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
