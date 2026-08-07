"""End-to-end Plaky status-sync smoke test.

Drives the FULL lifecycle through the real webhook endpoint against the real Plaky board and
asserts the item's Status/Type/Priority/assignee after every step — not just QA assignment.
This is the regression net for "does the task actually reflect where the work is at".

    poetry run python scripts/lifecycle_smoke.py            # run once
    poetry run python scripts/lifecycle_smoke.py --loop 5   # run repeatedly

Uses a synthetic issue/PR number pair so it never touches real GitHub artifacts; the Plaky
item it creates is deleted at the end unless --keep is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from typing import Any

import httpx

API = "http://localhost:8090/api/v1/webhooks/github"
BOARD = "269031"
REPO = {"full_name": "Team-Deepiri/deepiri-boardman", "name": "deepiri-boardman"}

# Each STATUS-type column has its OWN option vocabulary — decoding Type or Priority with the
# Status map is how "Bug" reads as "Continuous". Keyed by column title.
OPTION_NAMES = {
    "Status": {
        "0": "NEEDS ASSIGNED", "8": "Assigned", "2": "In Progress", "3": "Paused",
        "4": "Needs QA", "11": "Needs QA Again", "5": "In QA", "6": "QA Verified",
        "7": "QA Rejected", "1": "Completed", "9": "Deployed", "10": "Continuous",
    },
    "Type": {"0": "Story", "9": "Task", "10": "Bug", "12": "Research"},
    "Priority": {"0": "VERY IMPORTANT", "1": "High", "2": "Medium", "3": "Low"},
}


async def _post(client: httpx.AsyncClient, event: str, delivery: str, payload: dict) -> dict:
    r = await client.post(
        API,
        headers={"X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
                 "Content-Type": "application/json"},
        content=json.dumps(payload),
    )
    try:
        return r.json()
    except Exception:
        return {"ok": False, "raw": r.text[:200]}


async def _item_fields(task_id: str, *, expect: str = "", tries: int = 4) -> dict[str, Any]:
    """Read item fields, retrying briefly when `expect` (a column title) is still empty.

    Plaky is read-after-write eventually consistent: a person/status patch that returned
    ok=True can still be absent from the very next GET. Asserting instantly produced flaky
    "QA not assigned" failures even though the write had succeeded.
    """
    for attempt in range(tries):
        out = await _read_item_fields(task_id)
        if not expect or out.get(expect):
            return out
        await asyncio.sleep(1.5 * (attempt + 1))
    return out


async def _read_item_fields(task_id: str) -> dict[str, Any]:
    from boardman.plaky.client import PlakyClient

    res = await PlakyClient().get_board_item_public(BOARD, task_id)
    item = res.get("item") or {}
    out: dict[str, Any] = {"title": item.get("title")}
    for f in item.get("fields") or []:
        title, val = f.get("title"), f.get("value")
        if f.get("type") == "STATUS":
            out[title] = OPTION_NAMES.get(title, {}).get(str(val), val)
        elif f.get("type") == "PERSON":
            users = (val or {}).get("assignedUsers") if isinstance(val, dict) else None
            out[title] = users or []
    return out


async def run_once(*, keep: bool) -> bool:
    n = random.randint(900_000, 999_999)          # synthetic issue/PR number
    pr_n = n + 1
    issue_url = f"https://github.com/{REPO['full_name']}/issues/{n}"
    pr_url = f"https://github.com/{REPO['full_name']}/pull/{pr_n}"
    ok = True
    checks: list[tuple[str, str, str, bool]] = []

    def check(step: str, field: str, got: Any, want: str) -> None:
        nonlocal ok
        passed = str(got) == want
        ok = ok and passed
        checks.append((step, field, f"{got}", passed))

    async with httpx.AsyncClient(timeout=180.0) as c:
        # 1. issue opened -> task created, NEEDS ASSIGNED, no QA yet
        r = await _post(c, "issues", f"lc-{n}-open", {
            "action": "opened",
            "issue": {"number": n, "title": f"[lifecycle-smoke] payment retry crash {n}",
                      "body": "Crash in the retry handler.", "html_url": issue_url,
                      "labels": [{"name": "bug"}]},
            "repository": REPO,
        })
        task_id = str(r.get("plaky_task_id") or "")
        if not task_id:
            print(f"FAILED at issue-open: {json.dumps(r)[:200]}")
            return False
        f = await _item_fields(task_id)
        check("issue opened", "Status", f.get("Status"), "NEEDS ASSIGNED")
        check("issue opened", "Type", f.get("Type"), "Bug")
        check("issue opened", "Priority", f.get("Priority"), "High")
        check("issue opened", "QA (must be empty)", f.get("QA Engineer Assigned"), "[]")

        pr_body = {"number": pr_n, "title": "Fix payment retry crash",
                   "body": f"Fixes #{n}", "html_url": pr_url, "state": "open",
                   "merged": False, "draft": False, "user": {"login": "Blasted-ctrl"},
                   "head": {"ref": f"fix/{n}-retry"}, "labels": []}

        # 2. PR opened -> dev assigned, QA picked, Needs QA
        await _post(c, "pull_request", f"lc-{n}-pr", {"action": "opened",
                                                      "pull_request": pr_body, "repository": REPO})
        f = await _item_fields(task_id, expect="QA Engineer Assigned")
        check("PR opened", "Status", f.get("Status"), "Needs QA")
        check("PR opened", "Assignee set", bool(f.get("Assignee")), "True")
        check("PR opened", "QA assigned", bool(f.get("QA Engineer Assigned")), "True")
        qa_ids = f.get("QA Engineer Assigned") or []

        # 3. QA requests changes -> QA Rejected (only the assigned QA counts)
        from boardman.assignment.config import load_team_assignments

        cfg = load_team_assignments()
        qa_login = next(
            (m.github_login for m in cfg.members if str(m.id) in {str(x) for x in qa_ids}), ""
        )
        review = {"state": "changes_requested", "user": {"login": qa_login or "unknown"}}
        await _post(c, "pull_request_review", f"lc-{n}-rej", {
            "action": "submitted", "review": review,
            "pull_request": {**pr_body, "state": "open"}, "repository": REPO})
        f = await _item_fields(task_id)
        check("QA rejects", "Status", f.get("Status"), "QA Rejected")

        # 4. dev pushes a fix -> In Progress
        await _post(c, "pull_request", f"lc-{n}-sync", {"action": "synchronize",
                                                        "pull_request": pr_body, "repository": REPO})
        f = await _item_fields(task_id)
        check("dev pushes fix", "Status", f.get("Status"), "In Progress")

        # 5. QA comments -> In QA
        await _post(c, "pull_request_review", f"lc-{n}-cmt", {
            "action": "submitted", "review": {"state": "commented", "user": {"login": qa_login}},
            "pull_request": pr_body, "repository": REPO})
        f = await _item_fields(task_id)
        check("QA comments", "Status", f.get("Status"), "In QA")

        # 6. approval -> QA Verified
        await _post(c, "pull_request_review", f"lc-{n}-apr", {
            "action": "submitted", "review": {"state": "approved", "user": {"login": qa_login}},
            "pull_request": pr_body, "repository": REPO})
        f = await _item_fields(task_id)
        check("QA approves", "Status", f.get("Status"), "QA Verified")

        # 7. merge -> Completed
        await _post(c, "pull_request", f"lc-{n}-merge", {
            "action": "closed",
            "pull_request": {**pr_body, "state": "closed", "merged": True},
            "repository": REPO})
        f = await _item_fields(task_id)
        check("PR merged", "Status", f.get("Status"), "Completed")

        # 8. issue reopened -> back to In Progress
        await _post(c, "issues", f"lc-{n}-reopen", {
            "action": "reopened",
            "issue": {"number": n, "title": "x", "html_url": issue_url},
            "repository": REPO})
        f = await _item_fields(task_id)
        check("issue reopened", "Status", f.get("Status"), "In Progress")

    width = max(len(s) for s, _, _, _ in checks)
    for step, field, got, passed in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {step:<{width}}  {field:<22} = {got}")

    if not keep:
        from boardman.plaky.client import PlakyClient

        await PlakyClient().delete_board_item(BOARD, task_id)
        print(f"  cleaned up Plaky item {task_id}")
    else:
        print(f"  kept Plaky item {task_id}")
    return ok


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=1, help="how many full lifecycles to run")
    ap.add_argument("--keep", action="store_true", help="do not delete the Plaky items")
    a = ap.parse_args()

    passed = 0
    for i in range(1, a.loop + 1):
        print(f"\n=== lifecycle run {i}/{a.loop} ===")
        t0 = time.time()
        if await run_once(keep=a.keep):
            passed += 1
        print(f"  ({time.time() - t0:.0f}s)")
    print(f"\n{passed}/{a.loop} full lifecycles passed")
    return 0 if passed == a.loop else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
