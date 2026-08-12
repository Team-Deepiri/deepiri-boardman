"""Exhaustive GitHub -> Plaky automation matrix.

Drives EVERY documented transition through the real webhook endpoint against the real
Plaky board and asserts the item after each one. lifecycle_smoke.py covers the happy
path; this covers the whole state machine, including the branches that only fire from a
specific starting status (draft reversal, dismissal, resume-after-rejection) and the two
actions GitHub only sends as webhooks.

    poetry run python scripts/plaky_automation_matrix.py
    poetry run python scripts/plaky_automation_matrix.py --loop 3 --keep

Uses synthetic issue/PR numbers so no real GitHub artifact is touched. The Plaky item is
deleted at the end unless --keep.
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

OPTION_NAMES = {
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
    "Type": {"0": "Story", "9": "Task", "10": "Bug", "12": "Research"},
    "Priority": {"0": "VERY IMPORTANT", "1": "High", "2": "Medium", "3": "Low"},
}


async def _post(c: httpx.AsyncClient, event: str, delivery: str, payload: dict) -> dict:
    r = await c.post(
        API,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "Content-Type": "application/json",
        },
        content=json.dumps(payload),
    )
    try:
        return r.json()
    except Exception:
        return {"ok": False, "raw": r.text[:200]}


async def _read(task_id: str) -> dict[str, Any]:
    from boardman.plaky.client import PlakyClient

    res = await PlakyClient().get_board_item_public(BOARD, task_id)
    item = res.get("item") or {}
    out: dict[str, Any] = {"comments": item.get("commentCount") or 0}
    for f in item.get("fields") or []:
        title, val = f.get("title"), f.get("value")
        if f.get("type") == "STATUS":
            out[title] = OPTION_NAMES.get(title, {}).get(str(val), val)
        elif f.get("type") == "PERSON":
            users = (val or {}).get("assignedUsers") if isinstance(val, dict) else None
            out[title] = users or []
    return out


async def _status(task_id: str, want: str, tries: int = 4) -> dict[str, Any]:
    """Read with a short retry — Plaky is read-after-write eventually consistent."""
    out = await _read(task_id)
    for attempt in range(tries - 1):
        if out.get("Status") == want:
            return out
        await asyncio.sleep(1.2 * (attempt + 1))
        out = await _read(task_id)
    return out


async def run_once(*, keep: bool) -> tuple[int, int, list[str]]:
    n = random.randint(700_000, 799_999)
    pr_n = n + 1
    iss_url = f"https://github.com/{REPO['full_name']}/issues/{n}"
    pr_url = f"https://github.com/{REPO['full_name']}/pull/{pr_n}"
    rows: list[tuple[str, str, bool]] = []
    failures: list[str] = []

    def check(step: str, got: Any, want: str) -> None:
        passed = str(got) == str(want)
        rows.append((step, f"{got}", passed))
        if not passed:
            failures.append(f"{step}: got {got!r}, want {want!r}")

    def pr(**over: Any) -> dict:
        base = {
            "number": pr_n,
            "title": "Fix payment retry crash",
            "body": f"Fixes #{n}",
            "html_url": pr_url,
            "state": "open",
            "merged": False,
            "draft": False,
            "user": {"login": "Blasted-ctrl"},
            "head": {"ref": f"fix/{n}-retry"},
            "labels": [],
        }
        base.update(over)
        return base

    def issue_comment(body: str, login: str) -> dict:
        return {
            "action": "created",
            "issue": {
                "number": pr_n,
                "title": "Fix",
                "html_url": pr_url,
                "pull_request": {"url": pr_url},
            },
            "comment": {"user": {"login": login}, "body": body, "html_url": pr_url + "#c"},
            "repository": REPO,
        }

    async with httpx.AsyncClient(timeout=180.0) as c:
        # ---- issue lifecycle -------------------------------------------------
        r = await _post(
            c,
            "issues",
            f"mx-{n}-open",
            {
                "action": "opened",
                "issue": {
                    "number": n,
                    "title": f"[matrix] payment retry crash {n}",
                    "body": "Crash in the retry handler.",
                    "html_url": iss_url,
                    "labels": [{"name": "bug"}],
                },
                "repository": REPO,
            },
        )
        task_id = str(r.get("plaky_task_id") or "")
        if not task_id:
            return 0, 1, [f"issue-open failed: {json.dumps(r)[:200]}"]

        f = await _read(task_id)
        check("1  issue opened -> NEEDS ASSIGNED", f.get("Status"), "NEEDS ASSIGNED")
        check("2  issue opened -> Type from label", f.get("Type"), "Bug")
        check("3  issue opened -> Priority inferred", f.get("Priority"), "High")
        check("4  issue opened -> NO QA yet", f.get("QA Engineer Assigned"), "[]")
        before = f.get("comments") or 0

        # comment on the plain ISSUE mirrors onto the task
        await _post(
            c,
            "issue_comment",
            f"mx-{n}-icmt",
            {
                "action": "created",
                "issue": {"number": n, "title": "t", "html_url": iss_url},
                "comment": {
                    "user": {"login": "Blasted-ctrl"},
                    "body": "repro confirmed on staging",
                    "html_url": iss_url + "#c1",
                },
                "repository": REPO,
            },
        )
        await asyncio.sleep(2)
        f = await _read(task_id)
        check("5  issue comment -> mirrored", (f.get("comments") or 0) > before, "True")

        # ---- PR with NO "Fixes #n": the fuzzy-link pipeline ------------------
        # Every other PR here says "Fixes #n", so the regex always matched and the whole
        # auto_link/llm_link path never ran — yet that is how most real PRs arrive. If
        # fuzzy linking breaks, PRs silently stop reaching their task and nobody notices,
        # because the PRs that DO carry a closing keyword keep working.
        fuzzy_pr = pr_n + 5000
        link_res = await _post(
            c,
            "pull_request",
            f"mx-{n}-fuzzy",
            {
                "action": "opened",
                "pull_request": pr(
                    number=fuzzy_pr,
                    title=f"[matrix] payment retry crash {n}",
                    body="Refactors the retry handler. No issue reference on purpose.",
                    html_url=f"https://github.com/{REPO['full_name']}/pull/{fuzzy_pr}",
                ),
                "repository": REPO,
            },
        )
        linked_ids = [str(x.get("task_id")) for x in (link_res.get("linked") or [])]
        check("5a  PR without 'Fixes #n' still links", task_id in linked_ids, "True")
        check(
            "5b  linked by title/branch, not regex",
            str(link_res.get("pipeline") or "") in ("auto_link", "llm_link"),
            "True",
        )
        f = await _status(task_id, "Needs QA")
        check("5c  fuzzy-linked PR -> Needs QA", f.get("Status"), "Needs QA")
        check("5d  fuzzy-linked PR -> QA assigned", bool(f.get("QA Engineer Assigned")), "True")

        # ---- PR opens as DRAFT: QA assigned, but NOT moved to Needs QA -------
        await _post(
            c,
            "pull_request",
            f"mx-{n}-draft",
            {"action": "opened", "pull_request": pr(draft=True), "repository": REPO},
        )
        f = await _read(task_id)
        for _ in range(4):
            if f.get("QA Engineer Assigned"):
                break
            await asyncio.sleep(1.5)
            f = await _read(task_id)
        check("6  draft PR -> dev assigned", bool(f.get("Assignee")), "True")
        check("7  draft PR -> QA assigned", bool(f.get("QA Engineer Assigned")), "True")
        check("8  draft PR -> NOT Needs QA yet", f.get("Status") != "Needs QA", "True")
        qa_ids = f.get("QA Engineer Assigned") or []

        from boardman.assignment.config import load_team_assignments

        cfg = load_team_assignments()
        qa = next((m for m in cfg.members if str(m.id) in {str(x) for x in qa_ids}), None)
        qa_login = (getattr(qa, "github_login", "") or "") if qa else ""
        check(
            "9  QA is not an excluded lead",
            (getattr(qa, "display", "") or "") not in (cfg.qa_excluded or []),
            "True",
        )

        # ---- draft <-> ready transitions ------------------------------------
        await _post(
            c,
            "pull_request",
            f"mx-{n}-ready",
            {"action": "ready_for_review", "pull_request": pr(), "repository": REPO},
        )
        check(
            "10 ready_for_review -> Needs QA",
            (await _status(task_id, "Needs QA")).get("Status"),
            "Needs QA",
        )

        await _post(
            c,
            "pull_request",
            f"mx-{n}-todraft",
            {"action": "converted_to_draft", "pull_request": pr(draft=True), "repository": REPO},
        )
        check(
            "11 converted_to_draft -> In Progress",
            (await _status(task_id, "In Progress")).get("Status"),
            "In Progress",
        )

        await _post(
            c,
            "pull_request",
            f"mx-{n}-ready2",
            {"action": "ready_for_review", "pull_request": pr(), "repository": REPO},
        )
        check(
            "12 ready again -> Needs QA",
            (await _status(task_id, "Needs QA")).get("Status"),
            "Needs QA",
        )

        # ---- review_requested ------------------------------------------------
        await _post(
            c,
            "pull_request",
            f"mx-{n}-revreq",
            {
                "action": "review_requested",
                "pull_request": pr(requested_reviewers=[{"login": qa_login or "qa"}]),
                "repository": REPO,
            },
        )
        check(
            "13 review_requested -> In QA", (await _status(task_id, "In QA")).get("Status"), "In QA"
        )

        # Editing an already-linked PR (retitle, checklist added) must be a no-op. Rerunning
        # the open pipeline would drag the task back from In QA and re-post the "PR Opened"
        # comment every time someone fixes a typo in their description.
        before_edit = await _read(task_id)
        edited = await _post(
            c,
            "pull_request",
            f"mx-{n}-edited",
            {
                "action": "edited",
                "pull_request": pr(
                    title="Fix payment retry crash (typo fix)",
                    body=f"Fixes #{n}\n\nAdded a checklist.",
                ),
                "repository": REPO,
            },
        )
        await asyncio.sleep(2)
        after_edit = await _read(task_id)
        check("13b edit on a linked PR is skipped", edited.get("skipped") is True, "True")
        check("13c edit did not churn status", after_edit.get("Status"), "In QA")
        check(
            "13d edit did not re-comment",
            (after_edit.get("comments") or 0) == (before_edit.get("comments") or 0),
            "True",
        )
        check(
            "13e edit did not churn QA",
            str(after_edit.get("QA Engineer Assigned")),
            str(before_edit.get("QA Engineer Assigned")),
        )

        # ---- QA rejects / dev resumes ---------------------------------------
        await _post(
            c,
            "pull_request_review",
            f"mx-{n}-rej",
            {
                "action": "submitted",
                "review": {"state": "changes_requested", "user": {"login": qa_login or "unknown"}},
                "pull_request": pr(),
                "repository": REPO,
            },
        )
        check(
            "14 QA requests changes -> QA Rejected",
            (await _status(task_id, "QA Rejected")).get("Status"),
            "QA Rejected",
        )

        await _post(
            c,
            "pull_request",
            f"mx-{n}-sync",
            {"action": "synchronize", "pull_request": pr(), "repository": REPO},
        )
        check(
            "15 dev pushes fix -> In Progress",
            (await _status(task_id, "In Progress")).get("Status"),
            "In Progress",
        )

        # ---- comment-driven transitions -------------------------------------
        await _post(
            c,
            "issue_comment",
            f"mx-{n}-pause",
            issue_comment("let's pause this until the API contract lands", "Blasted-ctrl"),
        )
        check(
            "16 'pause' comment -> Paused",
            (await _status(task_id, "Paused")).get("Status"),
            "Paused",
        )

        await _post(
            c,
            "issue_comment",
            f"mx-{n}-ping",
            issue_comment(f"@{qa_login or 'qa'} ready for another look", "someone-not-qa"),
        )
        check(
            "17 dev pings QA -> Needs QA Again",
            (await _status(task_id, "Needs QA Again")).get("Status"),
            "Needs QA Again",
        )

        await _post(
            c,
            "pull_request_review",
            f"mx-{n}-cmt",
            {
                "action": "submitted",
                "review": {"state": "commented", "user": {"login": qa_login or "qa"}},
                "pull_request": pr(),
                "repository": REPO,
            },
        )
        check("18 QA comments -> In QA", (await _status(task_id, "In QA")).get("Status"), "In QA")

        # ---- approve / dismiss / approve -------------------------------------
        await _post(
            c,
            "pull_request_review",
            f"mx-{n}-apr",
            {
                "action": "submitted",
                "review": {"state": "approved", "user": {"login": "christiankrider1"}},
                "pull_request": pr(),
                "repository": REPO,
            },
        )
        check(
            "19 approval -> QA Verified",
            (await _status(task_id, "QA Verified")).get("Status"),
            "QA Verified",
        )

        await _post(
            c,
            "pull_request_review",
            f"mx-{n}-dis",
            {
                "action": "dismissed",
                "review": {"state": "dismissed", "user": {"login": "christiankrider1"}},
                "pull_request": pr(),
                "repository": REPO,
            },
        )
        check(
            "20 approval dismissed -> In QA",
            (await _status(task_id, "In QA")).get("Status"),
            "In QA",
        )

        await _post(
            c,
            "pull_request_review",
            f"mx-{n}-apr2",
            {
                "action": "submitted",
                "review": {"state": "approved", "user": {"login": "christiankrider1"}},
                "pull_request": pr(),
                "repository": REPO,
            },
        )
        check(
            "21 re-approved -> QA Verified",
            (await _status(task_id, "QA Verified")).get("Status"),
            "QA Verified",
        )

        # ---- merge / issue close / reopen ------------------------------------
        await _post(
            c,
            "pull_request",
            f"mx-{n}-merge",
            {
                "action": "closed",
                "pull_request": pr(state="closed", merged=True),
                "repository": REPO,
            },
        )
        check(
            "22 PR merged -> Completed",
            (await _status(task_id, "Completed")).get("Status"),
            "Completed",
        )

        await _post(
            c,
            "issues",
            f"mx-{n}-reopen",
            {
                "action": "reopened",
                "issue": {"number": n, "title": "t", "html_url": iss_url},
                "repository": REPO,
            },
        )
        check(
            "23 issue reopened -> In Progress",
            (await _status(task_id, "In Progress")).get("Status"),
            "In Progress",
        )

        await _post(
            c,
            "issues",
            f"mx-{n}-close",
            {
                "action": "closed",
                "issue": {"number": n, "title": "t", "html_url": iss_url},
                "repository": REPO,
            },
        )
        check(
            "24 issue closed -> Completed",
            (await _status(task_id, "Completed")).get("Status"),
            "Completed",
        )

    width = max(len(s) for s, _, _ in rows)
    for step, got, passed in rows:
        print(f"  {'PASS' if passed else 'FAIL'}  {step:<{width}}  = {got}")

    if not keep:
        from boardman.plaky.client import PlakyClient

        await PlakyClient().delete_board_item(BOARD, task_id)
        print(f"  cleaned up Plaky item {task_id}")
    else:
        print(f"  kept Plaky item {task_id}")

    passed_n = sum(1 for _, _, p in rows if p)
    return passed_n, len(rows), failures


async def run_edge_cases(*, keep: bool) -> tuple[int, int, list[str]]:
    """Guard rails that are easy to regress and invisible in the happy path."""
    n = random.randint(800_000, 899_999)
    pr_a, pr_b = n + 1, n + 2
    iss_url = f"https://github.com/{REPO['full_name']}/issues/{n}"
    rows: list[tuple[str, str, bool]] = []
    failures: list[str] = []

    def check(step: str, got: Any, want: str) -> None:
        passed = str(got) == str(want)
        rows.append((step, f"{got}", passed))
        if not passed:
            failures.append(f"{step}: got {got!r}, want {want!r}")

    def pr(num: int, **over: Any) -> dict:
        base = {
            "number": num,
            "title": "Fix retry",
            "body": f"Fixes #{n}",
            "html_url": f"https://github.com/{REPO['full_name']}/pull/{num}",
            "state": "open",
            "merged": False,
            "draft": False,
            "user": {"login": "Blasted-ctrl"},
            "head": {"ref": f"fix/{n}-x"},
            "labels": [],
        }
        base.update(over)
        return base

    async with httpx.AsyncClient(timeout=180.0) as c:
        r = await _post(
            c,
            "issues",
            f"edge-{n}-open",
            {
                "action": "opened",
                "issue": {
                    "number": n,
                    "title": f"[matrix-edge] retry {n}",
                    "body": "b",
                    "html_url": iss_url,
                    "labels": [],
                },
                "repository": REPO,
            },
        )
        task_id = str(r.get("plaky_task_id") or "")
        if not task_id:
            return 0, 1, [f"edge issue-open failed: {json.dumps(r)[:200]}"]

        # 1. duplicate webhook delivery must not create a second task
        dup = await _post(
            c,
            "issues",
            f"edge-{n}-open",
            {
                "action": "opened",
                "issue": {"number": n, "title": "dup", "body": "b", "html_url": iss_url},
                "repository": REPO,
            },
        )
        check("E1 duplicate delivery ignored", "Duplicate" in str(dup.get("message") or ""), "True")

        # 2. same issue re-sent with a NEW delivery id must still not duplicate the task
        again = await _post(
            c,
            "issues",
            f"edge-{n}-open2",
            {
                "action": "opened",
                "issue": {"number": n, "title": "dup2", "body": "b", "html_url": iss_url},
                "repository": REPO,
            },
        )
        check(
            "E2 issue re-open dedupes by mapping",
            "already mapped" in str(again.get("message") or "").lower(),
            "True",
        )

        # 3. two PRs on one task: merging only the first must NOT complete it
        await _post(
            c,
            "pull_request",
            f"edge-{n}-prA",
            {"action": "opened", "pull_request": pr(pr_a), "repository": REPO},
        )

        # A second PR on the same task must not re-pick QA or steal the assignee. Both
        # fields are curated once and then relied on: silently swapping the QA mid-review
        # sends the work to someone who never agreed to it, and the board still looks fine.
        after_a = await _read(task_id)
        for _ in range(4):
            if after_a.get("QA Engineer Assigned"):
                break
            await asyncio.sleep(1.5)
            after_a = await _read(task_id)
        qa_a = after_a.get("QA Engineer Assigned") or []
        asg_a = after_a.get("Assignee") or []
        status_a = after_a.get("Status")
        check("E3a QA assigned on 1st PR", bool(qa_a), "True")

        await _post(
            c,
            "pull_request",
            f"edge-{n}-prB",
            {
                "action": "opened",
                # different author: the engineer field must still not be overwritten
                "pull_request": pr(pr_b, user={"login": "christiankrider1"}),
                "repository": REPO,
            },
        )
        await asyncio.sleep(2)
        after_b = await _read(task_id)
        check(
            "E3b 2nd PR did not re-pick QA", (after_b.get("QA Engineer Assigned") or []), str(qa_a)
        )
        check("E3c 2nd PR did not steal the assignee", (after_b.get("Assignee") or []), str(asg_a))
        check("E3d 2nd PR did not reset Status", after_b.get("Status"), str(status_a))
        await _post(
            c,
            "pull_request",
            f"edge-{n}-mergeA",
            {
                "action": "closed",
                "pull_request": pr(pr_a, state="closed", merged=True),
                "repository": REPO,
            },
        )
        f = await _read(task_id)
        check("E3 1st of 2 PRs merged -> NOT Completed", f.get("Status") != "Completed", "True")

        # 4. a review from someone who is NOT the assigned QA must not reject the task
        before = (await _read(task_id)).get("Status")
        await _post(
            c,
            "pull_request_review",
            f"edge-{n}-badrej",
            {
                "action": "submitted",
                "review": {
                    "state": "changes_requested",
                    "user": {"login": "definitely-not-the-qa"},
                },
                "pull_request": pr(pr_b),
                "repository": REPO,
            },
        )
        after = (await _read(task_id)).get("Status")
        check("E4 non-assigned QA rejection ignored", after == before, "True")

        # 5. merging the LAST open PR completes the task
        await _post(
            c,
            "pull_request",
            f"edge-{n}-mergeB",
            {
                "action": "closed",
                "pull_request": pr(pr_b, state="closed", merged=True),
                "repository": REPO,
            },
        )
        check(
            "E5 all PRs merged -> Completed",
            (await _status(task_id, "Completed")).get("Status"),
            "Completed",
        )

        # 6. an unknown event type is refused, not silently accepted
        unknown = await _post(
            c, "workflow_run", f"edge-{n}-unknown", {"action": "completed", "repository": REPO}
        )
        check("E6 unsupported event refused", unknown.get("ok") is False, "True")

        # 7. Boardman's own QA-assignment comment must not drive the state machine.
        # It comments as the PAT owner (a support-team human, not a "[bot]" login), so
        # without the marker guard the comment comes straight back through the feed and
        # moves the task to In QA before the assigned QA has opened anything.
        from boardman.github.pr_actions import with_marker

        before = (await _read(task_id)).get("Status")
        own = await _post(
            c,
            "issue_comment",
            f"edge-{n}-selfcomment",
            {
                "action": "created",
                "issue": {"number": pr_b, "title": "t", "html_url": iss_url, "pull_request": {}},
                "comment": {
                    "user": {"login": "Blasted-ctrl"},
                    "body": with_marker("@someone you've been assigned as **QA reviewer**"),
                    "html_url": f"{iss_url}#issuecomment-edge-self-{n}",
                },
                "repository": REPO,
            },
        )
        check("E7 Boardman ignores its own comment", own.get("skipped") is True, "True")
        check(
            "E7b own comment did not move status",
            (await _read(task_id)).get("Status") == before,
            "True",
        )

        # 8. a restart replays the catch-up window. Status writes are idempotent, but
        # comments are additive: the same comment must not be mirrored onto the task twice.
        replay_url = f"{iss_url}#issuecomment-edge-replay-{n}"
        replay_body = {
            "action": "created",
            "issue": {"number": n, "title": "t", "html_url": iss_url},
            "comment": {
                "user": {"login": "Blasted-ctrl"},
                "body": "QA note: replayed by a restart",
                "html_url": replay_url,
            },
            "repository": REPO,
        }
        before_comments = (await _read(task_id)).get("comments") or 0
        await _post(c, "issue_comment", f"edge-{n}-replay1", replay_body)
        once = (await _read(task_id)).get("comments") or 0
        await _post(c, "issue_comment", f"edge-{n}-replay2", replay_body)
        twice = (await _read(task_id)).get("comments") or 0
        check("E8 comment mirrored once", once > before_comments, "True")
        check("E8b replay did not re-post it", twice == once, "True")

    width = max(len(s) for s, _, _ in rows)
    for step, got, passed in rows:
        print(f"  {'PASS' if passed else 'FAIL'}  {step:<{width}}  = {got}")

    if not keep:
        from boardman.plaky.client import PlakyClient

        await PlakyClient().delete_board_item(BOARD, task_id)
        print(f"  cleaned up Plaky item {task_id}")

    return sum(1 for _, _, p in rows if p), len(rows), failures


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=1)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--edge", action="store_true", help="run the edge-case guards instead")
    a = ap.parse_args()

    total_p = total_n = 0
    all_failures: list[str] = []
    for i in range(1, a.loop + 1):
        print(f"\n=== automation matrix run {i}/{a.loop} ===")
        t0 = time.time()
        p, n, fails = await (run_edge_cases(keep=a.keep) if a.edge else run_once(keep=a.keep))
        total_p += p
        total_n += n
        all_failures.extend(f"run{i} {x}" for x in fails)
        print(f"  {p}/{n} checks passed ({time.time() - t0:.0f}s)")

    print(f"\nTOTAL: {total_p}/{total_n} checks passed across {a.loop} run(s)")
    if all_failures:
        print("\nFAILURES:")
        for x in all_failures:
            print("  -", x)
    return 0 if total_p == total_n else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
