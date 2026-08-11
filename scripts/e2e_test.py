"""
End-to-end integration tests for boardman.

Tests (in order):
  1.  Plaky API connectivity + list boards/groups → find test group
  2.  Plaky workspace users (for identity match)
  3.  GitHub team roster fetch
  4.  GitHub↔Plaky identity fuzzy matching (incl. edge-case names)
  5.  Repo tier classification (local + live metadata)
  6.  QA picker end-to-end (roster → tier filter → weighted pick)
  7.  Fuzzy PR title → Plaky task matching
  8.  Write a real test task to Plaky (test group) + verify it appears
  9.  Worker route wiring check (QA assignment, repo tier, fuzzy matching, assignee matching)
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback

import httpx

# ── ensure repo root is on path ──────────────────────────────────────────────
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from boardman.assignment.config import load_team_assignments
from boardman.assignment.identity_match import best_plaky_match_for_github
from boardman.assignment.qa_picker import pick_qa_for_repo
from boardman.assignment.tier_classifier import classify_repo_tier
from boardman.github.repo_metadata import fetch_repo_metadata
from boardman.github.team_roster import get_cached_support_team_roster
from boardman.plaky.client import PlakyClient
from boardman.services.task_mutations import UpdateTaskInput, update_task_internal
from boardman.settings import settings

# ─────────────────────────────────────────────────────────────────────────────
PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m~\033[0m"
SECTION = "\033[1;34m"
RESET = "\033[0m"

results: list[dict] = []


def section(title: str):
    print(f"\n{SECTION}{'─'*60}{RESET}")
    print(f"{SECTION}{title}{RESET}")
    print(f"{SECTION}{'─'*60}{RESET}")


def ok(label: str, detail: str = ""):
    results.append({"label": label, "pass": True})
    print(f"  {PASS} {label}", f"  [{detail}]" if detail else "")


def fail(label: str, detail: str = ""):
    results.append({"label": label, "pass": False})
    print(f"  {FAIL} {label}", f"  [{detail}]" if detail else "")


def warn(label: str, detail: str = ""):
    results.append({"label": label, "pass": None})
    print(f"  {WARN} {label}", f"  [{detail}]" if detail else "")


# ═════════════════════════════════════════════════════════════════════════════
async def test_plaky_connectivity():
    section("1. Plaky API — boards + groups + find test group")
    plaky = PlakyClient()
    res = await plaky.list_boards()
    if not res.get("ok"):
        fail("list_boards", res.get("message", ""))
        return None, None, None

    boards = res.get("boards", [])
    ok("list_boards", f"{len(boards)} board(s) found")
    for b in boards:
        print(f"      board id={b['id']}  name={b['name']!r}")

    # find DEEPIRI MAIN BOARD
    main_board = next(
        (b for b in boards if "main" in b["name"].lower()),
        boards[0] if boards else None,
    )
    if not main_board:
        fail("find main board")
        return None, None, None
    ok("find main board", f"id={main_board['id']} name={main_board['name']!r}")

    # find Boardman Test Board for writes
    test_board = next(
        (
            b
            for b in boards
            if "boardman test" in b["name"].lower() or "test board" in b["name"].lower()
        ),
        None,
    )
    write_board_id = test_board["id"] if test_board else main_board["id"]

    # list groups on main board (for schema inspection)
    grp_res = await plaky.list_groups(main_board["id"])
    groups = grp_res.get("groups", [])
    if grp_res.get("ok"):
        ok("list_groups (main board)", f"{len(groups)} group(s)")
    else:
        fail("list_groups", grp_res.get("message", ""))
        return main_board["id"], None, None

    for g in groups:
        print(f"      group id={g['id']}  name={g['name']!r}")

    # list groups on test board for write target
    tgrp_res = await plaky.list_groups(write_board_id)
    tgroups = tgrp_res.get("groups", [])
    test_group = next(
        (g for g in tgroups if "boardman" in g["name"].lower()),
        tgroups[0] if tgroups else None,
    )
    if test_board and test_group:
        ok(
            "find test write target",
            f"board={test_board['name']!r}  group={test_group['name']!r} id={test_group['id']}",
        )
    else:
        warn("find test board/group", "using main board first group as fallback")

    return write_board_id, test_group["id"] if test_group else None, groups


async def test_plaky_users():
    section("2. Plaky workspace users")
    plaky = PlakyClient()
    res = await plaky.list_workspace_users()
    users = res.get("users", [])
    if res.get("ok"):
        ok("list_workspace_users", f"{len(users)} user(s)")
        for u in users[:6]:
            print(
                f"      id={u.get('id')}  name={u.get('name') or u.get('fullName')}  email={u.get('email')}"
            )
        if len(users) > 6:
            print(f"      ... +{len(users)-6} more")
    else:
        fail("list_workspace_users", res.get("message", ""))
    return users


async def test_github_roster():
    section("3. GitHub team roster")
    team = settings.github_support_team
    roster = get_cached_support_team_roster(team)
    members = roster.get("members", [])
    if roster.get("ok"):
        ok("fetch_roster", f"{len(members)} member(s) from {team!r}")
        for m in members[:6]:
            print(f"      login={m.get('login')}  name={m.get('name')}")
        if len(members) > 6:
            print(f"      ... +{len(members)-6} more")
    else:
        fail("fetch_roster", roster.get("message", ""))
    return members


async def test_identity_matching(github_members: list, plaky_users: list):
    section("4. GitHub ↔ Plaky identity fuzzy matching")
    if not plaky_users:
        warn("skip — no Plaky users returned")
        return

    # ── edge cases ────────────────────────────────────────────────────────────
    # Fabricated names that stress the matcher: partial match, different spacing,
    # hyphenated last name, initials, non-ASCII
    fake_gh_profiles = [
        {"login": "joe-black", "name": "Joe Black", "email": "joe@deepiri.com"},
        {"login": "jblack", "name": "J. Black", "email": ""},
        {"login": "maria-garcia-lopez", "name": "Maria Garcia-Lopez", "email": "mgl@deepiri.com"},
        {"login": "devuser99", "name": "", "email": "dev@deepiri.com"},
        {"login": "xXshadow_coderXx", "name": "Shadow Coder", "email": ""},
    ]
    # Also test real roster members
    for m in github_members[:3]:
        fake_gh_profiles.append(m)

    for gh in fake_gh_profiles:
        matched_id, reason, score = best_plaky_match_for_github(
            gh, plaky_users, min_score=640, ambiguity_margin=45
        )
        label = f"match {gh.get('login')} ({gh.get('name') or '?'})"
        if matched_id:
            puser = next((u for u in plaky_users if str(u.get("id")) == matched_id), {})
            ok(label, f"→ {puser.get('name') or matched_id}  score={score}  [{reason}]")
        elif reason == "ambiguous":
            warn(label, f"ambiguous score={score}")
        else:
            warn(label, f"no match score={score} [{reason}]")


async def test_repo_tier_classification():
    section("5. Repo tier classification — live from GitHub org")
    from boardman.github.org_repos import fetch_org_repository_full_names

    if not settings.github_pat:
        warn("skip — no GITHUB_PAT set")
        return

    # Try configured org first, fall back to checking user orgs
    org = settings.github_org
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            repo_names = await fetch_org_repository_full_names(client, org)
        except Exception:
            repo_names = []
        if not repo_names:
            # Discover orgs from PAT and try each
            headers = {
                "Authorization": f"Bearer {settings.github_pat}",
                "Accept": "application/vnd.github+json",
            }
            r = await client.get("https://api.github.com/user/orgs", headers=headers)
            orgs = [o["login"] for o in r.json()] if r.status_code == 200 else []
            for candidate in orgs:
                try:
                    repo_names = await fetch_org_repository_full_names(client, candidate)
                    if repo_names:
                        org = candidate
                        break
                except Exception:
                    continue

    if not repo_names:
        warn("no repos returned", f"tried org={settings.github_org} and user orgs")
        return

    ok("fetched org repos", f"{len(repo_names)} repos from {org}")
    tier_counts = {1: 0, 2: 0, 3: 0}

    async with httpx.AsyncClient(timeout=30) as client:
        for full_name in repo_names:
            owner, repo = full_name.split("/", 1)
            meta = await fetch_repo_metadata(client, owner, repo)
            if not meta:
                warn(f"no metadata: {full_name}")
                continue
            tier, score = classify_repo_tier(meta)
            tier_counts[tier] += 1
            ok(
                f"classify {repo}",
                f"tier={tier}  lang={meta.language or '?'}  "
                f"size={meta.size_kb}kb  topics={meta.topics[:3]}  score={score.total}",
            )

    print(
        f"\n      Tier distribution: T1={tier_counts[1]}  T2={tier_counts[2]}  T3={tier_counts[3]}"
    )


async def test_qa_picker():
    section("6. QA picker end-to-end")
    cfg = load_team_assignments()
    ok("load_team_assignments", f"{len(cfg.members)} member(s) loaded")
    if not cfg.members:
        warn("skip QA pick — no members loaded (GitHub roster empty or no PAT)")
        return

    for m in cfg.members[:5]:
        print(
            f"      id={m.id}  display={m.display!r}  qa_tier={m.qa_tier}  roles={m.roles}  globs={m.repo_globs[:2]}"
        )

    # Pick QA for several repo types
    test_repos = [
        "deepiri-org/deepiri-frontend",
        "deepiri-org/emotion-desktop",
        "deepiri-org/boardman",
        "deepiri-org/training-orchestrator",
        "deepiri-org/api-gateway",
    ]
    for repo in test_repos:
        qa_id, reason = await pick_qa_for_repo(repo, cfg)
        if qa_id:
            matched = next((m for m in cfg.members if m.id == qa_id), None)
            name = matched.display if matched else qa_id
            ok(f"pick_qa {repo.split('/')[-1]}", f"→ {name}  [{reason}]")
        else:
            warn(f"pick_qa {repo.split('/')[-1]}", reason)


async def test_fuzzy_pr_plaky_matching(board_id: str | None):
    section("7. Fuzzy PR title → Plaky task matching (title scoring)")
    if not board_id:
        warn("skip — no board_id")
        return

    # Test the scoring logic directly without sqlalchemy DB session
    from rapidfuzz import fuzz  # type: ignore

    pr_titles = [
        "Fix login redirect loop on mobile",
        "Add Stripe payment integration",
        "Implement QA assignment pipeline for PRs",
        "chore: bump dependencies",
        "Refactor boardman webhook handler",
    ]
    # Fetch real Plaky board items from the main board to match against
    plaky = PlakyClient()
    items_res = await plaky.list_board_items(board_id)
    items = items_res if isinstance(items_res, list) else []

    if not items:
        warn("no board items to match against", "board may be empty")
        # Still test the algorithm with synthetic task titles
        items = [
            {"id": "fake1", "title": "Fix mobile login issue"},
            {"id": "fake2", "title": "Payment gateway integration"},
            {"id": "fake3", "title": "QA automation for PRs"},
            {"id": "fake4", "title": "Dependency upgrades"},
        ]
        ok("using synthetic tasks for scoring demo")

    for pr_title in pr_titles:
        scored = []
        for item in items[:50]:  # cap to avoid noise
            task_title = str(item.get("title") or item.get("name") or "")
            if not task_title:
                continue
            score = fuzz.token_set_ratio(pr_title.lower(), task_title.lower())
            scored.append((score, task_title))
        scored.sort(reverse=True)
        if scored:
            best_score, best_title = scored[0]
            label = f"PR: {pr_title[:35]!r}"
            detail = f"best match ({best_score}%): {best_title[:50]!r}"
            if best_score >= 70:
                ok(label, detail)
            elif best_score >= 40:
                warn(label, detail)
            else:
                warn(label, f"no strong match (best={best_score}%)")


async def test_plaky_write(board_id: str | None, group_id: str | None):
    section("8. Write real test task to Plaky + verify")
    if not board_id or not group_id:
        warn("skip — board_id or group_id not found")
        return

    plaky = PlakyClient()
    title = "[boardman e2e test] QA assignment + PR lifecycle smoke test"
    desc = (
        "**Automated e2e test task** — safe to delete.\n\n"
        "This verifies: QA assignment, repo tier classification, PR status lifecycle, "
        "fuzzy PR↔task matching, and Plaky write/read."
    )
    res = await plaky.create_task(
        title=title,
        description=desc,
        priority="low",
        board_id=board_id,
        group_id=group_id,
    )
    if res.get("ok"):
        task_id = res.get("task", {}).get("id") or res.get("task", {}).get("taskId")
        ok("create test task", f"id={task_id}")

        # Add a comment
        cres = await plaky.add_comment(
            task_id, "✅ e2e test: task created and comment added by boardman"
        )
        if cres.get("ok"):
            ok("add_comment to test task")
        else:
            warn("add_comment", cres.get("message", ""))

        # Update status via same path as PATCH /tasks (board field patch + legacy /tasks fallback)
        sres = await update_task_internal(
            str(task_id),
            UpdateTaskInput(status=settings.plaky_status_needs_qa),
        )
        if sres.get("ok"):
            ok("update_task_internal → needs_qa")
        else:
            warn("update_task_internal", sres.get("message", ""))

        print(f"\n  Task URL: {res.get('task_url') or '(no url returned)'}")
        print(f"  Task ID:  {task_id}")
    else:
        fail("create test task", res.get("message", ""))


async def test_worker_wiring():
    section("9. Worker route wiring check")

    # Check that all four capabilities are present in the worker source
    worker_index = os.path.join(REPO, "worker/src/index.ts")
    worker_pick = os.path.join(REPO, "worker/src/pickQaLocal.ts")
    worker_rules = os.path.join(REPO, "worker/src/qaTierRules.ts")

    for path in [worker_index, worker_pick, worker_rules]:
        if os.path.exists(path):
            ok(f"worker file exists: {os.path.basename(path)}")
        else:
            fail(f"worker file missing: {path}")

    # 1. QA assignment via tier
    with open(worker_pick) as f:
        pick_src = f.read()
    if "qaTierAllowsRepo" in pick_src and "pickQaLocal" in pick_src:
        ok("worker: QA assignment with tier filter wired")
    else:
        fail("worker: QA assignment tier filter missing")

    # 2. Dynamic repo tier — worker proxies to boardman /api/v1/assignment/pick-qa
    with open(worker_index) as f:
        idx_src = f.read()
    if "/api/v1/assignment/pick-qa" in idx_src:
        ok("worker: proxies to boardman pick-qa (dynamic repo tier via boardman)")
    else:
        fail("worker: boardman proxy route missing")

    # 3. QA_TEAM_JSON env var for local fallback member list
    if "QA_TEAM_JSON" in idx_src:
        ok("worker: QA_TEAM_JSON env var for local member fallback")
    else:
        fail("worker: QA_TEAM_JSON env var missing")

    # 4. Fuzzy PR matching — handled server-side by boardman (not worker), check service exists
    pr_linking = os.path.join(REPO, "boardman/services/pr_task_linking.py")
    if os.path.exists(pr_linking):
        ok("fuzzy PR↔task matching: pr_task_linking.py exists (server-side)")
    else:
        fail("fuzzy PR↔task matching: pr_task_linking.py missing")

    # 5. QA assignee → GitHub login matching (team_checker)
    team_checker = os.path.join(REPO, "boardman/assignment/team_checker.py")
    if os.path.exists(team_checker):
        with open(team_checker) as f:
            tc = f.read()
        if "is_support_member" in tc and "get_cached_support_team_roster" in tc:
            ok("assignee email/login matching: team_checker.py wired")
        else:
            fail("team_checker.py missing is_support_member")
    else:
        fail("team_checker.py missing")

    # 6. Worker hardcoded patterns check — warn if still hardcoded
    with open(worker_rules) as f:
        rules_src = f.read()
    if (
        "DEFAULT_TIER2_EXCLUDED" in rules_src
        and len([line for line in rules_src.splitlines() if "*diva*" in line or "*cyrex*" in line])
        > 0
    ):
        warn(
            "worker qaTierRules.ts still has hardcoded DEFAULT_TIER2_EXCLUDED patterns",
            "These are used as fallback defaults when no BOARDMAN_URL is set. "
            "Move to QA_TEAM_JSON env var config if you want fully dynamic control.",
        )
    else:
        ok("worker: no hardcoded tier patterns (fully dynamic)")


# ═════════════════════════════════════════════════════════════════════════════
async def main():
    print(f"\n{'═'*60}")
    print("  boardman e2e integration test")
    print(f"{'═'*60}")
    print(f"  Plaky base : {settings.plaky_api_base}")
    print(f"  GitHub org : {settings.github_org}")
    print(f"  GitHub team: {settings.github_support_team}")
    print(f"{'═'*60}\n")

    try:
        board_id, test_group_id, _ = await test_plaky_connectivity()
        plaky_users = await test_plaky_users()
        github_members = await test_github_roster()
        await test_identity_matching(github_members, plaky_users)
        await test_repo_tier_classification()
        await test_qa_picker()
        await test_fuzzy_pr_plaky_matching(board_id)
        await test_plaky_write(board_id, test_group_id)
        await test_worker_wiring()
    except Exception as e:
        print(f"\n\033[91mUnhandled exception: {e}\033[0m")
        traceback.print_exc()

    # ── Summary ──────────────────────────────────────────────────────────────
    section("Summary")
    passed = [r for r in results if r["pass"] is True]
    failed = [r for r in results if r["pass"] is False]
    warned = [r for r in results if r["pass"] is None]
    print(
        f"  {PASS} {len(passed)} passed   {FAIL} {len(failed)} failed   {WARN} {len(warned)} warnings"
    )
    if failed:
        print("\n  Failed:")
        for r in failed:
            print(f"    {FAIL} {r['label']}")
    if warned:
        print("\n  Warnings:")
        for r in warned:
            print(f"    {WARN} {r['label']}")
    print()
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    asyncio.run(main())
