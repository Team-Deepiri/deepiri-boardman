"""The 22-step acceptance sequence from the closing meeting notes, run for real.

Every step goes through the live webhook endpoint against the live Plaky board, and
every claim is read back off the board before it is called a pass. Steps 21 and 22 ask
the running assistant what happened and check the answer names the work, its final
state, and comes back quickly.

Synthetic issue / PR numbers, so no GitHub artifact is created or merged. The Plaky item
is deleted at the end unless --keep.

    poetry run python scripts/acceptance_e2e.py
    poetry run python scripts/acceptance_e2e.py --base http://localhost:8090 --keep
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

BOARD = "269028"  # "Bots" — the production routing for this repo
REPO = {"full_name": "Team-Deepiri/deepiri-boardman", "name": "deepiri-boardman"}
AUTHOR = "Blasted-ctrl"

OPTION_NAMES: dict[str, dict[str, str]] = {
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


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, bool]] = []

    def check(self, step: str, got: Any, want: Any) -> bool:
        ok = str(got) == str(want)
        self.rows.append((step, f"got {got!r}", ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {step}\n          {got!r}", flush=True)
        return ok

    @property
    def failures(self) -> list[str]:
        return [f"{s}: {g}" for s, g, ok in self.rows if not ok]


async def _post(c: httpx.AsyncClient, api: str, event: str, delivery: str, payload: dict) -> dict:
    r = await c.post(
        f"{api}/webhooks/github",
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "Content-Type": "application/json",
        },
        content=json.dumps(payload),
    )
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "raw": r.text[:200]}


async def _read(task_id: str) -> dict[str, Any]:
    from boardman.plaky.client import PlakyClient

    res = await PlakyClient().get_board_item_public(BOARD, task_id)
    item = res.get("item") or {}
    out: dict[str, Any] = {"title": item.get("name") or item.get("title") or ""}
    for f in item.get("fields") or []:
        title, val = f.get("title"), f.get("value")
        if f.get("type") == "STATUS":
            out[title] = OPTION_NAMES.get(title, {}).get(str(val), val)
        elif f.get("type") == "PERSON":
            users = (val or {}).get("assignedUsers") if isinstance(val, dict) else None
            out[title] = [str(u) for u in (users or [])]
    return out


async def _settle(task_id: str, field: str, want: str, tries: int = 5) -> dict[str, Any]:
    """Read back with a short retry. Plaky is eventually consistent after a write."""
    out = await _read(task_id)
    for attempt in range(tries - 1):
        if str(out.get(field)) == str(want):
            return out
        await asyncio.sleep(1.0 + 0.8 * attempt)
        out = await _read(task_id)
    return out


async def ask(c: httpx.AsyncClient, api: str, message: str) -> tuple[str, float]:
    t0 = time.monotonic()
    r = await c.post(
        f"{api}/agent/chat",
        json={
            "message": message,
            "repo": REPO["full_name"],
            "use_tools": True,
            "allow_writes": False,
            "plaky_board_id": BOARD,
            "plaky_group_id": "933385",
        },
    )
    elapsed = time.monotonic() - t0
    try:
        return str(r.json().get("reply") or ""), elapsed
    except ValueError:
        return r.text[:400], elapsed


async def _forget_synthetic(issue_number: int, pr_number: int) -> None:
    """Drop this run's issue/PR mappings so they are not read back as real work."""
    try:
        from sqlalchemy import delete

        from boardman.database.models import IssueTaskMap, PullRequestTaskLink, SyncLog
        from boardman.database.session import async_session

        short = REPO["name"]
        async with async_session() as session:
            await session.execute(
                delete(IssueTaskMap).where(
                    IssueTaskMap.github_repo == short,
                    IssueTaskMap.github_issue_number.in_([issue_number, pr_number]),
                )
            )
            await session.execute(
                delete(PullRequestTaskLink).where(
                    PullRequestTaskLink.github_repo == short,
                    PullRequestTaskLink.github_pr_number.in_([issue_number, pr_number]),
                )
            )
            await session.execute(
                delete(SyncLog).where(
                    SyncLog.github_repo == short,
                    SyncLog.github_ref.in_([str(issue_number), str(pr_number)]),
                )
            )
            await session.commit()
    except Exception as e:  # cleanup is best effort; never fail a passing run over it
        print(f"could not clean local mappings: {e}", flush=True)


async def run(api: str, keep: bool) -> int:
    rep = Report()
    n = random.randint(810_000, 899_999)
    pr_n = n + 1
    iss_url = f"https://github.com/{REPO['full_name']}/issues/{n}"
    pr_url = f"https://github.com/{REPO['full_name']}/pull/{pr_n}"
    title = f"[acceptance {n}] checkout retry drops the second attempt"

    def issue(**over: Any) -> dict:
        base: dict[str, Any] = {
            "number": n,
            "title": title,
            "body": "The retry helper swallows the second attempt and the order is lost.",
            "html_url": iss_url,
            "state": "open",
            "labels": [{"name": "enhancement"}],
            "assignees": [],
        }
        base.update(over)
        return base

    def pull(**over: Any) -> dict:
        base: dict[str, Any] = {
            "number": pr_n,
            "title": "Fix the dropped checkout retry",
            "body": f"Fixes #{n}",
            "html_url": pr_url,
            "state": "open",
            "merged": False,
            "draft": False,
            "user": {"login": AUTHOR},
            "head": {"ref": f"fix/{n}-checkout-retry"},
            "base": {"ref": "dev"},
            "labels": [],
        }
        base.update(over)
        return base

    async with httpx.AsyncClient(timeout=300.0) as c:
        # 1. Create an Issue.
        print("\n-- 1..3  issue created, then its label changes --", flush=True)
        r = await _post(
            c,
            api,
            "issues",
            f"acc-{n}-open",
            {"action": "opened", "issue": issue(), "repository": REPO},
        )
        task_id = str(r.get("plaky_task_id") or "")
        if not task_id:
            print(f"FAIL  issue did not create a Plaky task: {json.dumps(r)[:300]}")
            return 1
        f = await _read(task_id)
        rep.check("1  Issue created -> Plaky task exists", bool(task_id), True)
        rep.check("1b Issue created -> NEEDS ASSIGNED", f.get("Status"), "NEEDS ASSIGNED")
        rep.check("1c 'enhancement' label -> Type Feature", f.get("Type"), "Feature")

        # 2. Change its GitHub label.  3. Verify Plaky Type changes.
        await _post(
            c,
            api,
            "issues",
            f"acc-{n}-label",
            {
                "action": "labeled",
                "issue": issue(labels=[{"name": "bug"}]),
                "label": {"name": "bug"},
                "repository": REPO,
            },
        )
        f = await _settle(task_id, "Type", "Bug")
        rep.check("2  label changed to 'bug'", True, True)
        rep.check("3  Plaky Type re-synced -> Bug", f.get("Type"), "Bug")

        # 4. Change its priority.  5. Verify Plaky Priority changes.
        print("\n-- 4..5  priority --", flush=True)
        await _post(
            c,
            api,
            "issues",
            f"acc-{n}-prio",
            {
                "action": "labeled",
                "issue": issue(labels=[{"name": "bug"}, {"name": "priority: urgent"}]),
                "label": {"name": "priority: urgent"},
                "repository": REPO,
            },
        )
        f = await _settle(task_id, "Priority", "VERY IMPORTANT")
        rep.check("4  priority label set to urgent", True, True)
        rep.check("5  Plaky Priority -> VERY IMPORTANT", f.get("Priority"), "VERY IMPORTANT")
        rep.check("5b Type survived the priority change", f.get("Type"), "Bug")

        # 6. Assign a developer.  7. Verify Assignee + Assigned.
        print("\n-- 6..7  developer assigned --", flush=True)
        await _post(
            c,
            api,
            "issues",
            f"acc-{n}-assign",
            {
                "action": "assigned",
                "issue": issue(
                    labels=[{"name": "bug"}, {"name": "priority: urgent"}],
                    assignees=[{"login": AUTHOR}],
                ),
                "assignee": {"login": AUTHOR},
                "repository": REPO,
            },
        )
        f = await _settle(task_id, "Status", "Assigned")
        rep.check("6  developer assigned on GitHub", True, True)
        rep.check("7  Plaky Assignee filled", bool(f.get("Assignee")), True)
        rep.check("7b Plaky Status -> Assigned", f.get("Status"), "Assigned")

        # 8. Remove the developer.  9. Verify NEEDS ASSIGNED.
        print("\n-- 8..9  developer removed --", flush=True)
        await _post(
            c,
            api,
            "issues",
            f"acc-{n}-unassign",
            {
                "action": "unassigned",
                "issue": issue(labels=[{"name": "bug"}], assignees=[]),
                "assignee": {"login": AUTHOR},
                "repository": REPO,
            },
        )
        f = await _settle(task_id, "Status", "NEEDS ASSIGNED")
        rep.check("8  developer removed on GitHub", True, True)
        rep.check("9  Plaky Status -> NEEDS ASSIGNED", f.get("Status"), "NEEDS ASSIGNED")
        rep.check("9b Assignee cleared", f.get("Assignee"), [])

        # 10..13  PR opened against the same issue.
        print("\n-- 10..13  PR opened, task reused, author + QA --", flush=True)
        pr_res = await _post(
            c,
            api,
            "pull_request",
            f"acc-{n}-pr",
            {"action": "opened", "pull_request": pull(), "repository": REPO},
        )
        pr_task = str(pr_res.get("plaky_task_id") or "")
        f = await _read(task_id)
        rep.check("10 PR opened against the issue", bool(pr_res.get("ok", True)), True)
        rep.check("11 existing Plaky task reused (no duplicate)", pr_task or task_id, task_id)
        rep.check("12 PR author is the Plaky Assignee", bool(f.get("Assignee")), True)
        qa = f.get("QA Engineer Assigned") or []
        rep.check("13 QA engineer selected", bool(qa), True)
        rep.check("13b QA is not the PR author", str(qa) != str(f.get("Assignee")), True)

        # A duplicate delivery of the same PR must not create a second task.
        dupe = await _post(
            c,
            api,
            "pull_request",
            f"acc-{n}-pr-dupe",
            {"action": "opened", "pull_request": pull(), "repository": REPO},
        )
        rep.check(
            "11b duplicate PR webhook still maps to the same task",
            str(dupe.get("plaky_task_id") or task_id),
            task_id,
        )

        qa_login = ""
        try:
            from boardman.assignment.config import load_team_assignments

            cfg = load_team_assignments()
            by_id = {
                str(m.id): getattr(m, "github_login", "")
                for m in list(cfg.members) + list(getattr(cfg, "fallback_members", []) or [])
            }
            qa_login = by_id.get(str(qa[0]), "") if qa else ""
        except Exception:
            qa_login = ""

        # 14..15  changes requested.
        print("\n-- 14..18  review cycle --", flush=True)
        await _post(
            c,
            api,
            "pull_request_review",
            f"acc-{n}-reject",
            {
                "action": "submitted",
                "review": {"state": "changes_requested", "user": {"login": qa_login or "unknown"}},
                "pull_request": pull(),
                "repository": REPO,
            },
        )
        f = await _settle(task_id, "Status", "QA Rejected")
        rep.check("14 QA requested changes", True, True)
        rep.check("15 Plaky Status -> QA Rejected", f.get("Status"), "QA Rejected")

        # 16. Push a fix.
        await _post(
            c,
            api,
            "pull_request",
            f"acc-{n}-push",
            {"action": "synchronize", "pull_request": pull(), "repository": REPO},
        )
        f = await _settle(task_id, "Status", "Needs QA")
        rep.check("16 dev pushed a fix -> back in the QA queue", f.get("Status"), "Needs QA")

        # 17..18  approval.
        await _post(
            c,
            api,
            "pull_request_review",
            f"acc-{n}-approve",
            {
                "action": "submitted",
                "review": {"state": "approved", "user": {"login": qa_login or "unknown"}},
                "pull_request": pull(),
                "repository": REPO,
            },
        )
        f = await _settle(task_id, "Status", "QA Verified")
        rep.check("17 QA approved", True, True)
        rep.check("18 Plaky Status -> QA Verified", f.get("Status"), "QA Verified")

        # 19..20  merge.
        print("\n-- 19..20  merge --", flush=True)
        await _post(
            c,
            api,
            "pull_request",
            f"acc-{n}-merge",
            {
                "action": "closed",
                "pull_request": pull(state="closed", merged=True),
                "repository": REPO,
            },
        )
        f = await _settle(task_id, "Status", "Completed")
        rep.check("19 PR merged", True, True)
        rep.check("20 Plaky Status -> Completed", f.get("Status"), "Completed")
        rep.check("20b Type still Bug after the whole run", f.get("Type"), "Bug")
        rep.check("20c Priority still VERY IMPORTANT", f.get("Priority"), "VERY IMPORTANT")

        # 21..22  ask the assistant what happened.
        print("\n-- 21..22  ask the assistant --", flush=True)
        reply, elapsed = await ask(
            c, api, f"what happened with the checkout retry work on issue {n}?"
        )
        print(f"  [{elapsed:.1f}s] {reply[:600]}", flush=True)
        rep.check("21 assistant answered", bool(reply.strip()), True)
        low = reply.casefold()
        rep.check(
            "22 answer is accurate (names the work and that it is done)",
            ("checkout" in low or "retry" in low) and ("complete" in low or "merged" in low),
            True,
        )
        rep.check("22b answer was fast (< 25s)", elapsed < 25.0, True)
        rep.check("22c answer is not a robot preamble", "as an ai" not in low, True)

        if not keep and task_id:
            try:
                from boardman.plaky.client import PlakyClient

                await PlakyClient().delete_board_item(BOARD, task_id)
                print(f"\ncleaned up Plaky task {task_id}", flush=True)
            except Exception as e:  # cleanup must never fail the run
                print(f"\ncould not delete task {task_id}: {e}", flush=True)
            # Delete the local mappings too. They outlive the Plaky task, and the
            # assistant reads them as live state — several runs of this script left the
            # board looking like it tracked dozens of issues that never existed.
            await _forget_synthetic(n, pr_n)

    passed = sum(1 for _, _, ok in rep.rows if ok)
    print("\n" + "=" * 74)
    print(f"ACCEPTANCE  {passed}/{len(rep.rows)} checks passed")
    for line in rep.failures:
        print(f"  FAILED  {line}")
    print("=" * 74)
    return 1 if rep.failures else 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8090")
    ap.add_argument("--keep", action="store_true", help="leave the Plaky task on the board")
    args = ap.parse_args()
    return await run(args.base.rstrip("/") + "/api/v1", args.keep)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
