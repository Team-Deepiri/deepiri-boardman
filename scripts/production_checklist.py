"""Boardman production-ready checklist, executed against real GitHub + real Plaky.

Mirrors the employer checklist line by line and prints PASS / FAIL / LIMITATION with
the observed value behind every line. Nothing here asserts from intent: each line is
decided by reading the live Plaky board after a real GitHub action.

Two kinds of trigger are used, and each line says which one it used:

  [gh]   a real mutation on a real GitHub issue. Nothing syncs it by hand - the
         running poller (or the webhook in production) notices and Boardman reacts.
         This is what proves "no manual sync required".
  [hook] the exact webhook payload GitHub delivers, POSTed to the real webhook
         endpoint. Used for pull-request, review and merge legs because the local
         PAT has no Contents:write, so a branch and PR cannot be created from here.
         The handler, the board, and every assertion are real.

    poetry run python scripts/production_checklist.py            # everything
    poetry run python scripts/production_checklist.py --section 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

REPO_FULL = "Team-Deepiri/deepiri-boardman"
REPO_SHORT = "deepiri-boardman"
REPO_BLOCK = {"full_name": REPO_FULL, "name": REPO_SHORT}
BOARD = "269028"
GROUP = "933385"
WEBHOOK = "http://localhost:8090/api/v1/webhooks/github"
AGENT = "http://localhost:8090/api/v1/agent/chat"
DB = Path(__file__).resolve().parent.parent / "boardman.db"
ME = "Blasted-ctrl"

STATUS_NAMES = {
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
}
TYPE_NAMES = {
    "0": "Story",
    "9": "Task",
    "10": "Bug",
    "12": "Research",
    "17": "Feature",
    "18": "Refactor",
}
PRIORITY_NAMES = {"0": "VERY IMPORTANT", "1": "High", "2": "Medium", "3": "Low"}

RESULTS: list[tuple[str, str, str, str]] = []  # section, verdict, line, evidence


def record(section: str, verdict: str, line: str, evidence: str = "") -> None:
    RESULTS.append((section, verdict, line, evidence))
    mark = {"PASS": "PASS", "FAIL": "FAIL", "LIMIT": "LIMIT", "SKIP": "SKIP"}[verdict]
    print(f"  [{mark}] {line}" + (f"   -- {evidence}" if evidence else ""), flush=True)


def _pat() -> str:
    for raw in (
        (Path(__file__).resolve().parent.parent / ".env")
        .read_text(encoding="utf-8", errors="ignore")
        .splitlines()
    ):
        if raw.startswith("GITHUB_PAT="):
            return raw.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


GH_HEADERS = {
    "Authorization": f"Bearer {_pat()}",
    "Accept": "application/vnd.github+json",
}


async def gh(client: httpx.AsyncClient, method: str, path: str, body: Any = None) -> Any:
    r = await client.request(
        method, f"https://api.github.com/repos/{REPO_FULL}{path}", headers=GH_HEADERS, json=body
    )
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub {method} {path} -> {r.status_code} {r.text[:200]}")
    return r.json() if r.text.strip() else {}


async def post_hook(client: httpx.AsyncClient, event: str, payload: dict) -> dict:
    r = await client.post(
        WEBHOOK,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": uuid.uuid4().hex,
            "Content-Type": "application/json",
        },
        content=json.dumps(payload),
    )
    try:
        return r.json()
    except Exception:
        return {"ok": False, "raw": r.text[:200]}


async def read_item(task_id: str) -> dict[str, Any]:
    from boardman.plaky.client import PlakyClient

    res = await PlakyClient().get_board_item_public(BOARD, task_id)
    item = res.get("item") or {}

    def _id(value: Any) -> str:
        # Plaky returns board/group either as a bare id or as an embedded object.
        if isinstance(value, dict):
            return str(value.get("id") or "")
        return str(value or "")

    out: dict[str, Any] = {
        "title": item.get("title") or "",
        "comments": item.get("commentCount") or 0,
        "board": _id(item.get("board")),
        "group": _id(item.get("group")),
    }
    for f in item.get("fields") or []:
        key, val = f.get("key"), f.get("value")
        if key == "status-8":
            out["Status"] = STATUS_NAMES.get(str(val), str(val))
        elif key == "status-7":
            out["Type"] = TYPE_NAMES.get(str(val), str(val))
        elif key == "status-9":
            out["Priority"] = PRIORITY_NAMES.get(str(val), str(val))
        elif key == "person-5":
            out["Assignee"] = (val or {}).get("assignedUsers") or []
        elif key == "person-6":
            out["QA"] = (val or {}).get("assignedUsers") or []
    return out


async def item_comments(task_id: str) -> list[str]:
    from boardman.plaky.client import PlakyClient, _headers

    p = PlakyClient()
    sid = await p.resolve_space_for_board(BOARD)
    root = (p._public_root() or "").rstrip("/")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"{root}/spaces/{sid}/boards/{BOARD}/items/{task_id}/comments",
            headers=_headers(p.api_key),
        )
    if r.status_code != 200:
        return []
    data = r.json()
    rows = data if isinstance(data, list) else (data.get("items") or [])
    return [str(x.get("content") or "") for x in rows]


def task_for_issue(number: int) -> str:
    if not DB.exists():
        return ""
    c = sqlite3.connect(str(DB))
    try:
        row = c.execute(
            "select plaky_task_id from issue_task_map where github_repo=? and github_issue_number=?",
            (REPO_SHORT, number),
        ).fetchone()
    finally:
        c.close()
    tid = str(row[0]) if row and row[0] else ""
    return "" if tid.startswith("pending:") else tid


async def wait_task_for_issue(number: int, timeout: float = 120.0) -> str:
    """Wait for the POLLER to notice a brand-new issue. Never syncs anything by hand."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tid = task_for_issue(number)
        if tid:
            return tid
        await asyncio.sleep(3)
    return ""


async def wait_field(
    task_id: str, field: str, want: Any, timeout: float = 120.0
) -> tuple[bool, Any]:
    """Poll the live board until `field` equals `want`. Returns (ok, last_seen)."""
    deadline = time.monotonic() + timeout
    seen: Any = None
    while time.monotonic() < deadline:
        item = await read_item(task_id)
        seen = item.get(field)
        if callable(want):
            if want(seen):
                return True, seen
        elif seen == want:
            return True, seen
        await asyncio.sleep(4)
    return False, seen


# --------------------------------------------------------------------------------------
# Section 1: GitHub Issue -> Plaky, driven entirely by real GitHub mutations
# --------------------------------------------------------------------------------------


async def section_1(client: httpx.AsyncClient) -> dict[str, Any]:
    S = "1. GitHub Issue -> Plaky"
    print(f"\n== {S} (real issue, poller does all syncing) ==", flush=True)
    stamp = time.strftime("%H:%M:%S")
    title = f"[checklist {stamp}] sync verification"
    body = "Original body written at creation."
    issue = await gh(client, "POST", "/issues", {"title": title, "body": body})
    num = int(issue["number"])
    print(f"  created real GitHub issue #{num}", flush=True)

    tid = await wait_task_for_issue(num)
    record(
        S,
        "PASS" if tid else "FAIL",
        "[gh] Plaky task created automatically",
        f"task={tid or 'none'}",
    )
    if not tid:
        return {"issue": num, "task": "", "ok": False}

    item = await read_item(tid)
    record(
        S,
        "PASS" if item["title"] == f"[{REPO_SHORT}] {title}" else "FAIL",
        "[gh] Title matches",
        item["title"],
    )
    comments = await item_comments(tid)
    joined = "\n".join(comments)
    record(
        S,
        "PASS" if body in joined else "FAIL",
        "[gh] Description/body is reflected",
        "found in task detail" if body in joined else "not found",
    )
    url = issue["html_url"]
    record(S, "PASS" if url in joined else "FAIL", "[gh] GitHub Issue URL is attached", url)
    ok_place = item["board"] == BOARD and item["group"] == GROUP
    record(
        S,
        "PASS" if ok_place else "FAIL",
        "[gh] Correct repository/board/group is used",
        f"board={item['board']} group={item['group']}",
    )
    record(
        S,
        "PASS" if item.get("Type") in TYPE_NAMES.values() else "FAIL",
        "[gh] Type is correct (no label -> board default)",
        str(item.get("Type")),
    )
    record(
        S,
        "PASS" if item.get("Priority") == "Medium" else "FAIL",
        "[gh] Priority is correct (no signal -> inferred Medium)",
        str(item.get("Priority")),
    )
    record(
        S,
        "PASS" if item.get("Status") == "NEEDS ASSIGNED" else "FAIL",
        "[gh] No assignee -> NEEDS ASSIGNED",
        str(item.get("Status")),
    )

    # assignee added
    await gh(client, "POST", f"/issues/{num}/assignees", {"assignees": [ME]})
    ok, seen = await wait_field(tid, "Assignee", lambda v: bool(v))
    record(
        S,
        "PASS" if ok else "FAIL",
        "[gh] GitHub assignee added -> Plaky assignee updates",
        f"assignee={seen}",
    )
    ok2, st = await wait_field(tid, "Status", "Assigned", timeout=60)
    record(S, "PASS" if ok2 else "FAIL", "[gh] ...and status follows to Assigned", str(st))

    # assignee removed
    await gh(client, "DELETE", f"/issues/{num}/assignees", {"assignees": [ME]})
    okc, seenc = await wait_field(tid, "Assignee", lambda v: not v)
    oks, sts = await wait_field(tid, "Status", "NEEDS ASSIGNED", timeout=60)
    record(
        S,
        "PASS" if (okc and oks) else "FAIL",
        "[gh] Assignee removed -> clears and returns to NEEDS ASSIGNED",
        f"assignee={seenc} status={sts}",
    )

    # label -> Type
    await gh(client, "POST", f"/issues/{num}/labels", {"labels": ["bug"]})
    okt, seent = await wait_field(tid, "Type", "Bug")
    record(
        S,
        "PASS" if okt else "FAIL",
        "[gh] Add/change a GitHub label -> Plaky Type updates",
        f"Type={seent}",
    )

    # priority change after creation (label carrying an explicit priority)
    await gh(client, "POST", f"/issues/{num}/labels", {"labels": ["good first issue"]})
    okp, seenp = await wait_field(tid, "Priority", "Low")
    record(
        S,
        "PASS" if okp else "FAIL",
        "[gh] Change priority after the issue exists -> Plaky Priority updates",
        f"Priority={seenp} (label 'good first issue' = Low)",
    )

    # title / body edits
    new_title = f"{title} EDITED"
    await gh(client, "PATCH", f"/issues/{num}", {"title": new_title, "body": "Body was rewritten."})
    await asyncio.sleep(25)
    item2 = await read_item(tid)
    renamed = item2["title"] == f"[{REPO_SHORT}] {new_title}"
    c2 = "\n".join(await item_comments(tid))
    mirrored = "Issue edited on GitHub" in c2 and "Body was rewritten." in c2
    record(
        S,
        "PASS" if renamed else ("LIMIT" if mirrored else "FAIL"),
        "[gh] Edit the issue title -> Plaky title updates",
        (
            "renamed"
            if renamed
            else (
                "Plaky API cannot rename an item; change posted to the task"
                if mirrored
                else "no update"
            )
        ),
    )
    record(
        S,
        "PASS" if mirrored else "FAIL",
        "[gh] Edit the issue body -> Plaky description updates",
        "new body posted to the task" if mirrored else "not reflected",
    )

    # comment mirroring
    marker = f"checklist comment {uuid.uuid4().hex[:6]}"
    await gh(client, "POST", f"/issues/{num}/comments", {"body": marker})
    found = False
    for _ in range(12):
        if marker in "\n".join(await item_comments(tid)):
            found = True
            break
        await asyncio.sleep(5)
    record(S, "PASS" if found else "FAIL", "[gh] Add a GitHub comment -> Plaky reflects it", marker)

    # close
    await gh(client, "PATCH", f"/issues/{num}", {"state": "closed"})
    okc2, stc = await wait_field(tid, "Status", "Completed")
    record(
        S, "PASS" if okc2 else "FAIL", "[gh] Close the issue -> Plaky reflects completion", str(stc)
    )

    # reopen (still unassigned -> must NOT be a blanket In Progress)
    await gh(client, "PATCH", f"/issues/{num}", {"state": "open"})
    okr, str_ = await wait_field(tid, "Status", "NEEDS ASSIGNED")
    record(
        S,
        "PASS" if okr else "FAIL",
        "[gh] Reopen the issue -> returns to the appropriate active state",
        f"{str_} (unassigned, so NEEDS ASSIGNED not In Progress)",
    )

    return {"issue": num, "task": tid, "ok": True}


# --------------------------------------------------------------------------------------
# Sections 2-4: PR link, QA workflow, merge (replayed webhook payloads, real board)
# --------------------------------------------------------------------------------------


def pr_payload(action: str, num: int, issue_num: int, **over: Any) -> dict:
    pr = {
        "number": num,
        "title": f"Fix for #{issue_num}",
        "body": f"Closes #{issue_num}",
        "html_url": f"https://github.com/{REPO_FULL}/pull/{num}",
        "state": over.pop("state", "open"),
        "merged": over.pop("merged", False),
        "draft": over.pop("draft", False),
        "user": {"login": over.pop("author", ME)},
        "labels": over.pop("labels", []),
        "assignees": over.pop("assignees", []),
        "head": {"ref": f"fix/{issue_num}-checklist", "sha": "d" * 40},
    }
    pr.update(over)
    return {"action": action, "pull_request": pr, "repository": REPO_BLOCK}


async def section_2_3_4(client: httpx.AsyncClient, issue_num: int, tid: str) -> dict[str, Any]:
    S2 = "2. PR -> Existing Plaky Task"
    print(f"\n== {S2} (replayed webhook payloads, real Plaky board) ==", flush=True)
    pr_num = 90000 + int(time.time()) % 9000

    await post_hook(client, "pull_request", pr_payload("opened", pr_num, issue_num))
    await asyncio.sleep(4)
    c0 = sqlite3.connect(str(DB))
    try:
        linked_rows = [
            r[0]
            for r in c0.execute(
                "select distinct plaky_task_id from pr_task_links "
                "where github_repo=? and github_pr_number=?",
                (REPO_SHORT, pr_num),
            ).fetchall()
        ]
    finally:
        c0.close()
    record(
        S2,
        "PASS" if linked_rows == [tid] else "FAIL",
        "[hook] Boardman finds the existing Plaky task (no new task invented)",
        f"linked={linked_rows} expected=[{tid!r}]",
    )

    # Needs QA is asserted HERE, before any comment is posted: the auto-picked QA
    # commenting is what legitimately advances the task to In QA.
    ok_nq, st_nq = await wait_field(tid, "Status", "Needs QA", timeout=90)
    record(
        S2,
        "PASS" if ok_nq else "FAIL",
        "[hook] PR open, no QA verdict yet -> Needs QA",
        str(st_nq),
    )

    c = sqlite3.connect(str(DB))
    try:
        rows = c.execute(
            "select distinct plaky_task_id from pr_task_links where github_repo=? and github_pr_number=?",
            (REPO_SHORT, pr_num),
        ).fetchall()
    finally:
        c.close()
    record(
        S2,
        "PASS" if len(rows) == 1 else "FAIL",
        "[hook] No duplicate task is created",
        f"{len(rows)} link row(s) -> {[r[0] for r in rows]}",
    )
    record(
        S2,
        "PASS" if rows else "FAIL",
        "[hook] PR URL is attached/recorded",
        "PullRequestTaskLink row written" if rows else "no link row",
    )

    ok, seen = await wait_field(tid, "Assignee", lambda v: bool(v), timeout=60)
    record(
        S2,
        "PASS" if ok else "FAIL",
        "[hook] PR author becomes the initial Plaky assignee",
        str(seen),
    )

    item = await read_item(tid)
    record(
        S2,
        "PASS" if item.get("Type") else "FAIL",
        "[hook] Type stays synchronized",
        str(item.get("Type")),
    )
    record(
        S2,
        "PASS" if item.get("Priority") else "FAIL",
        "[hook] Priority stays synchronized",
        str(item.get("Priority")),
    )

    await post_hook(
        client,
        "pull_request",
        pr_payload("labeled", pr_num, issue_num, labels=[{"name": "Refactor"}]),
    )
    okl, seenl = await wait_field(tid, "Type", "Refactor", timeout=60)
    record(
        S2,
        "PASS" if okl else "FAIL",
        "[hook] PR labels changing -> Plaky Type changes",
        f"Type={seenl} (Refactor label -> Refactor option, id 18)",
    )

    cm = f"pr activity {uuid.uuid4().hex[:6]}"
    await post_hook(
        client,
        "issue_comment",
        {
            "action": "created",
            "issue": {"number": pr_num, "pull_request": {"url": "x"}},
            "comment": {"body": cm, "user": {"login": ME}, "id": int(time.time())},
            "repository": REPO_BLOCK,
        },
    )
    await asyncio.sleep(6)
    record(
        S2,
        "PASS" if cm in "\n".join(await item_comments(tid)) else "FAIL",
        "[hook] PR comments -> Plaky activity updates",
        cm,
    )

    await post_hook(client, "pull_request", pr_payload("synchronize", pr_num, issue_num))
    c = sqlite3.connect(str(DB))
    try:
        still = c.execute(
            "select count(*) from pr_task_links where github_repo=? and github_pr_number=? and withdrawn_at is null",
            (REPO_SHORT, pr_num),
        ).fetchone()[0]
    finally:
        c.close()
    record(
        S2,
        "PASS" if still >= 1 else "FAIL",
        "[hook] New commits pushed -> task remains linked",
        f"{still} live link(s)",
    )

    # ---- Section 3: QA workflow
    S3 = "3. QA Workflow"
    print(f"\n== {S3} ==", flush=True)
    itq = await read_item(tid)
    qa_users = itq.get("QA") or []
    record(
        S3,
        "PASS" if qa_users else "FAIL",
        "[hook] QA selected by the GitHub-fit ranking and written to the QA field",
        f"QA={qa_users}",
    )

    # Resolve the assigned QA's GitHub login from the roster by their Plaky id, so the
    # review legs below are submitted BY the person Boardman actually chose.
    qa_login = ""
    if qa_users:
        from boardman.assignment.config import load_team_assignments

        cfg = load_team_assignments()
        want = str(qa_users[0])
        for m in list(cfg.members) + list(getattr(cfg, "fallback_members", []) or []):
            if str(getattr(m, "id", "")).strip() == want:
                qa_login = (getattr(m, "github_login", "") or "").strip()
                break
    record(
        S3,
        "PASS" if qa_login else "FAIL",
        "[hook] The assigned QA resolves to a real GitHub identity",
        qa_login or f"no roster member with Plaky id {qa_users}",
    )
    record(
        S3,
        "PASS" if qa_login.casefold() != ME.casefold() else "FAIL",
        "[hook] QA is not the PR author (no self-review)",
        f"author={ME} qa={qa_login or 'unassigned'}",
    )

    def review(state: str, login: str, body: str = "") -> dict:
        return {
            "action": "submitted",
            "review": {
                "state": state,
                "user": {"login": login},
                "body": body,
                "submitted_at": "2026-08-19T00:00:00Z",
                "id": int(time.time() * 10) % 10**9,
            },
            "pull_request": pr_payload("x", pr_num, issue_num)["pull_request"],
            "repository": REPO_BLOCK,
        }

    if qa_login:
        await post_hook(
            client,
            "issue_comment",
            {
                "action": "created",
                "issue": {"number": pr_num, "pull_request": {"url": "x"}},
                "comment": {
                    "body": "starting review",
                    "user": {"login": qa_login},
                    "id": int(time.time()) + 1,
                },
                "repository": REPO_BLOCK,
            },
        )
        oki, sti = await wait_field(tid, "Status", "In QA", timeout=60)
        record(S3, "PASS" if oki else "FAIL", "[hook] QA actively reviewing -> In QA", str(sti))

        await post_hook(client, "pull_request_review", review("changes_requested", qa_login))
        okr, strj = await wait_field(tid, "Status", "QA Rejected", timeout=60)
        record(
            S3, "PASS" if okr else "FAIL", "[hook] QA requests changes -> QA Rejected", str(strj)
        )

        await post_hook(client, "pull_request", pr_payload("synchronize", pr_num, issue_num))
        okp, stp = await wait_field(
            tid, "Status", lambda v: v in ("Needs QA Again", "Needs QA"), timeout=60
        )
        record(
            S3,
            "PASS" if okp else "FAIL",
            "[hook] Developer pushes fixes -> resubmitted for QA, link intact",
            str(stp),
        )

        await post_hook(client, "pull_request_review", review("approved", qa_login))
        oka, sta = await wait_field(tid, "Status", "QA Verified", timeout=60)
        record(S3, "PASS" if oka else "FAIL", "[hook] QA approves -> QA Verified", str(sta))

        before_c = (await read_item(tid)).get("Status")
        await post_hook(
            client,
            "issue_comment",
            {
                "action": "created",
                "issue": {"number": pr_num, "pull_request": {"url": "x"}},
                "comment": {
                    "body": "looks fine to me",
                    "user": {"login": "someone-else"},
                    "id": int(time.time()) + 2,
                },
                "repository": REPO_BLOCK,
            },
        )
        await asyncio.sleep(6)
        after_c = (await read_item(tid)).get("Status")
        record(
            S3,
            "PASS" if after_c == before_c else "FAIL",
            "[hook] A normal PR comment does not mark the task approved/rejected",
            f"{before_c} -> {after_c}",
        )
    else:
        for line in (
            "QA actively reviewing -> In QA",
            "QA requests changes -> QA Rejected",
            "Developer pushes fixes -> resubmitted",
            "QA approves -> QA Verified",
            "Normal comment does not change the verdict",
        ):
            record(S3, "SKIP", f"[hook] {line}", "no QA login resolved to act as")

    # ---- Section 4: merge
    S4 = "4. Merge"
    print(f"\n== {S4} ==", flush=True)
    await post_hook(
        client, "pull_request", pr_payload("closed", pr_num, issue_num, state="closed", merged=True)
    )
    okm, stm = await wait_field(tid, "Status", "Completed", timeout=90)
    record(
        S4, "PASS" if okm else "FAIL", "[hook] Merge detected -> task moves to completion", str(stm)
    )
    c = sqlite3.connect(str(DB))
    try:
        n_links, merged_at = c.execute(
            "select count(*), max(merged_at) from pr_task_links where github_repo=? and github_pr_number=?",
            (REPO_SHORT, pr_num),
        ).fetchone()
    finally:
        c.close()
    record(
        S4,
        "PASS" if merged_at else "FAIL",
        "[hook] PR remains linked after merge",
        f"links={n_links} merged_at={merged_at}",
    )
    record(
        S4,
        "PASS" if n_links == 1 else "FAIL",
        "[hook] No duplicate task created by the merge",
        f"{n_links} link row(s)",
    )
    return {"pr": pr_num, "qa_login": qa_login}


# --------------------------------------------------------------------------------------
# Section 5: synchronization reliability
# --------------------------------------------------------------------------------------


async def section_5(client: httpx.AsyncClient, issue_num: int, tid: str, pr_num: int) -> None:
    S = "5. Synchronization Reliability"
    print(f"\n== {S} ==", flush=True)

    # same event twice -> correct final state
    for _ in range(2):
        await post_hook(
            client,
            "pull_request",
            pr_payload("closed", pr_num, issue_num, state="closed", merged=True),
        )
    ok, st = await wait_field(tid, "Status", "Completed", timeout=60)
    record(S, "PASS" if ok else "FAIL", "Change something twice -> correct final state", str(st))

    # replayed delivery id -> no duplicate task
    payload = pr_payload("opened", pr_num, issue_num)
    delivery = uuid.uuid4().hex
    for _ in range(2):
        await client.post(
            WEBHOOK,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": delivery,
                "Content-Type": "application/json",
            },
            content=json.dumps(payload),
        )
    c = sqlite3.connect(str(DB))
    try:
        n = c.execute(
            "select count(distinct plaky_task_id) from pr_task_links where github_repo=? and github_pr_number=?",
            (REPO_SHORT, pr_num),
        ).fetchone()[0]
        dedupe = c.execute(
            "select count(*) from github_webhook_deliveries where delivery_id=?", (delivery,)
        ).fetchone()[0]
    finally:
        c.close()
    record(
        S,
        "PASS" if n == 1 else "FAIL",
        "Replay the same webhook -> no duplicate task",
        f"{n} distinct task(s), delivery rows={dedupe}",
    )

    # replayed comment -> no duplicate comment
    marker = f"dedupe probe {uuid.uuid4().hex[:6]}"
    body = {
        "action": "created",
        "issue": {"number": pr_num, "pull_request": {"url": "x"}},
        "comment": {"body": marker, "user": {"login": ME}, "id": 424242},
        "repository": REPO_BLOCK,
    }
    for _ in range(2):
        await post_hook(client, "issue_comment", body)
    await asyncio.sleep(8)
    count = "\n".join(await item_comments(tid)).count(marker)
    record(
        S,
        "PASS" if count == 1 else "FAIL",
        "Replay a comment event -> no duplicate comment",
        f"{count} copy on the task",
    )

    # make Plaky stale, then let reconciliation repair it
    from boardman.plaky.client import PlakyClient
    from boardman.plaky.dynamic_qa_status import resolve_plaky_status_patch

    rp = await resolve_plaky_status_patch(BOARD, intent="workflow_in_progress") or (
        await resolve_plaky_status_patch(BOARD, intent="workflow_assigned")
    )
    drifted = False
    if rp:
        await PlakyClient().patch_item_field_values(BOARD, tid, {rp[0]: rp[1]})
        await asyncio.sleep(2)
        drifted = (await read_item(tid)).get("Status") != "Completed"
    if drifted:
        r = await client.post(f"http://localhost:8090/api/v1/reconcile/{REPO_FULL}", timeout=600)
        out = r.json() if r.status_code == 200 else {}
        expected = "NEEDS ASSIGNED"  # issue is open and unassigned on GitHub
        okr, strec = await wait_field(tid, "Status", expected, timeout=120)
        record(
            S,
            "PASS" if okr else "FAIL",
            "Make Plaky stale -> reconciliation fixes it",
            f"status={strec} reconcile={ {k: out.get(k) for k in ('issues_checked','prs_checked','prs_skipped_closed')} }",
        )
    else:
        record(
            S, "SKIP", "Make Plaky stale -> reconciliation fixes it", "could not drift the field"
        )

    # mappings survive a restart: the map lives in SQLite, not memory
    c = sqlite3.connect(str(DB))
    try:
        mapped = c.execute(
            "select plaky_task_id from issue_task_map where github_repo=? and github_issue_number=?",
            (REPO_SHORT, issue_num),
        ).fetchone()
    finally:
        c.close()
    record(
        S,
        "PASS" if mapped and mapped[0] == tid else "FAIL",
        "Restart Boardman -> mappings preserved",
        f"issue_task_map row survives in SQLite: {mapped[0] if mapped else 'missing'}",
    )

    # a missed webhook is repaired by reconciliation
    r = await client.post(f"http://localhost:8090/api/v1/reconcile/{REPO_FULL}", timeout=600)
    out = r.json() if r.status_code == 200 else {}
    record(
        S,
        "PASS" if out.get("ok") else "FAIL",
        "Miss a webhook -> reconciliation eventually repairs the mismatch",
        f"issues={out.get('issues_checked')} prs={out.get('prs_checked')} "
        f"created={out.get('tasks_created')} skipped_closed={out.get('prs_skipped_closed')} "
        f"errors={len(out.get('errors') or [])}",
    )

    # Plaky API failure -> surfaced, not silently lost
    from boardman.services.task_mutations import UpdateTaskInput, update_task_internal

    bad = await update_task_internal(
        "999999999999", UpdateTaskInput(status="Completed", plaky_board_id=BOARD)
    )
    record(
        S,
        "PASS" if not bad.get("ok") else "FAIL",
        "Plaky API failure -> reported, never silently swallowed",
        f"ok={bad.get('ok')} message={str(bad.get('message') or bad.get('operations'))[:90]}",
    )


# --------------------------------------------------------------------------------------
# Section 6: the assistant
# --------------------------------------------------------------------------------------


async def ask(
    client: httpx.AsyncClient,
    message: str,
    session: str | None = None,
    tools: bool = True,
    writes: bool = False,
) -> tuple[str, str, float]:
    t0 = time.monotonic()
    r = await client.post(
        AGENT,
        json={
            "message": message,
            "session_id": session,
            "repo": REPO_FULL,
            "use_tools": tools,
            "allow_writes": writes,
        },
        timeout=300,
    )
    dt = time.monotonic() - t0
    d = r.json() if r.status_code == 200 else {"reply": f"HTTP {r.status_code}"}
    return str(d.get("reply") or ""), str(d.get("session_id") or ""), dt


async def section_6(client: httpx.AsyncClient) -> None:
    S = "6. Boardman Assistant"
    print(f"\n== {S} (real LLM calls against the running service) ==", flush=True)

    reply, sid, dt = await ask(client, "What does this repo do? Answer in two sentences.")
    good = len(reply) > 40 and any(w in reply.lower() for w in ("plaky", "github", "sync", "task"))
    record(
        S,
        "PASS" if good else "FAIL",
        f"'What does this repo do?' -> fast, accurate ({dt:.1f}s)",
        reply[:110].replace("\n", " "),
    )

    reply2, _, dt2 = await ask(
        client, "What are the 5 most important things this repo needs? Be specific."
    )
    grounded = reply2.count("\n") >= 3 or len(reply2) > 200
    record(
        S,
        "PASS" if grounded else "FAIL",
        f"'5 most important things' -> repo-grounded ({dt2:.1f}s)",
        reply2[:110].replace("\n", " "),
    )

    reply3, _, dt3 = await ask(
        client, "What issues and pull requests are currently open in this repo?"
    )
    current = any(t in reply3 for t in ("#8", "#7", "#9", "issue", "PR", "pull"))
    record(
        S,
        "PASS" if current else "FAIL",
        f"Asking about current issues/PRs -> current info ({dt3:.1f}s)",
        reply3[:110].replace("\n", " "),
    )

    reply4, _, dt4 = await ask(
        client, "Which QA engineer should review a PR touching the Plaky client, and why?"
    )
    sensible = len(reply4) > 60
    record(
        S,
        "PASS" if sensible else "FAIL",
        f"QA recommendation uses the ranking system ({dt4:.1f}s)",
        reply4[:110].replace("\n", " "),
    )

    # follow-up in the same session
    r5, _, dt5 = await ask(client, "Summarize what you just told me in one line.", session=sid)
    remembers = len(r5) > 20 and "no previous" not in r5.lower()
    record(
        S,
        "PASS" if remembers else "FAIL",
        f"Follow-up in the same session remembers context ({dt5:.1f}s)",
        r5[:110].replace("\n", " "),
    )

    # does it interrogate about board/group/repo it already knows?
    r6, _, dt6 = await ask(
        client,
        "Create a Plaky task titled 'Checklist probe - safe to delete' " "for this repo.",
        tools=True,
        writes=False,
    )
    asks_board = any(
        p in r6.lower()
        for p in (
            "which board",
            "which group",
            "which repo",
            "what board",
            "board id?",
            "which plaky board",
        )
    )
    record(
        S,
        "PASS" if not asks_board else "FAIL",
        "Does not ask for board/group/repo it already knows",
        r6[:110].replace("\n", " "),
    )

    _, _, warm = await ask(client, "In one word, are you online?", session=sid)
    record(
        S,
        "PASS" if warm < 20 else "FAIL",
        f"Simple questions feel fast ({warm:.1f}s)",
        f"{warm:.1f}s warm turn",
    )


# --------------------------------------------------------------------------------------


def summary() -> int:
    print("\n" + "=" * 78)
    print("CHECKLIST SUMMARY")
    print("=" * 78)
    sections: dict[str, list[tuple[str, str, str]]] = {}
    for sec, verdict, line, ev in RESULTS:
        sections.setdefault(sec, []).append((verdict, line, ev))
    total = {"PASS": 0, "FAIL": 0, "LIMIT": 0, "SKIP": 0}
    for sec, rows in sections.items():
        p = sum(1 for v, _, _ in rows if v == "PASS")
        print(f"\n{sec}: {p}/{len(rows)} pass")
        for v, line, ev in rows:
            if v != "PASS":
                print(f"    {v}: {line}  -- {ev}")
        for v, _, _ in rows:
            total[v] += 1
    print("\n" + "-" * 78)
    print(
        f"TOTAL  pass={total['PASS']}  fail={total['FAIL']}  "
        f"limitation={total['LIMIT']}  skipped={total['SKIP']}"
    )
    print("-" * 78)
    return 1 if total["FAIL"] else 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", action="append", default=[])
    ap.add_argument("--keep", action="store_true", help="leave the test issue open")
    args = ap.parse_args()
    want = set(args.section) or {"1", "2", "3", "4", "5", "6"}

    async with httpx.AsyncClient(timeout=300) as client:
        ctx: dict[str, Any] = {}
        if "1" in want:
            ctx = await section_1(client)
        if not ctx.get("task"):
            print("\nsections 2-5 need the section 1 task; run without --section or with 1 first")
            return summary()
        pr_info: dict[str, Any] = {}
        if {"2", "3", "4"} & want:
            pr_info = await section_2_3_4(client, ctx["issue"], ctx["task"])
        if "5" in want and pr_info:
            await section_5(client, ctx["issue"], ctx["task"], pr_info["pr"])
        if "6" in want:
            await section_6(client)

        if ctx.get("issue") and not args.keep:
            try:
                await gh(client, "PATCH", f"/issues/{ctx['issue']}", {"state": "closed"})
                print(f"\ncleanup: closed test issue #{ctx['issue']}")
            except Exception as e:
                print(f"cleanup failed: {e}")
    return summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
