# deepiri-boardman — Status Report


## 1. The Vision

**deepiri-boardman is the automation layer between GitHub and Plaky.** It removes the manual
bookkeeping QA and engineers do today: matching PRs to the right Plaky task, assigning the right
people, and keeping each task's status in sync with what's actually happening on the PR.

The goal: **a developer opens a PR and does nothing else in Plaky.** Boardman figures out which
task it belongs to, fills in who's working on it, picks a QA, and walks the task through its QA
lifecycle automatically as the PR is reviewed, paused, reworked, and merged.

It runs as a backend webhook worker — GitHub sends events, boardman updates Plaky. No human in
the loop.

---

## 2. The Desired Final Output

When a PR moves through its life, boardman drives the matching Plaky task through these states
**automatically**:

| Trigger (on GitHub) | Plaky result |
|---|---|
| Issue opened | Task created on the repo's board + **QA auto-assigned** by tier |
| PR opened, matched to a task | **Type** set from the branch (`feat/`→Feature, `fix/`→Bug…); **Assignee** filled from the PR author (if empty) → **Needs QA** |
| Assigned QA comments on the PR | **In QA** |
| Assigned QA requests changes | **QA Rejected** |
| Developer pushes new commits after a rejection | **In Progress** (resumed) |
| A comment says "pause" / "on hold" | **Paused** |
| Developer @-mentions the QA / support team | **Needs QA** (again) |
| Assigned QA approves the PR | **QA Verified** |
| PR merged | **Completed** |

Matching a PR to a task uses **title similarity + branch + identity** (GitHub login/email/name →
Plaky user). QA is chosen from a **tiered roster** (tier-3 QA can review any repo; tier-1 only
tier-1 repos), with repo tier inferred from the repo's structure.

Production runs **worker-only** — no chat UI, just the webhook automation.

---

## 3. Where We Are Now

**The full lifecycle above works end-to-end.** It was verified live against the `diri-cyrex`
Plaky board by simulating a complete PR: every transition fired and was confirmed by reading the
task back in Plaky:

```
NEEDS ASSIGNED (QA auto-assigned)
  → Needs QA (Type + Assignee filled)
  → In QA → QA Rejected → In Progress (resume)
  → Paused → Needs QA (re-ping) → QA Verified → Completed
```

The codebase is mature: identity matching, title/branch/cosine PR-task linking, the QA tier
system, and the status transitions are all implemented and covered by **289 automated tests**.

### Key fixes that made it work
A few foundational bugs were silently disabling automation and are now fixed:
- **Status options were invisible.** Plaky nests dropdown options (e.g. "In QA", "Completed")
  under a field's `configuration.values`; boardman wasn't reading them, so *no* status update
  ever applied. Now read correctly — this unblocked the entire status machine.
- **Item fields were read wrong.** Plaky returns an item's fields as a list, not a flat map, so
  "is this commenter the assigned QA?" always answered "no." Fixed — assigned-QA detection works.
- **Per-board field keys.** The category boards use different column keys (QA is `person-3` on one,
  `person-4` on another); boardman now resolves them per board from the live schema.
- **Renamed repos** (301 redirects) and a wrong `GITHUB_ORG` were breaking repo lookups — fixed.

---

## 4. What's Done ✅

- GitHub → Plaky webhook sync (issues, PRs, reviews, comments) with idempotent delivery handling
- PR → task matching by `Fixes #N`, and by title + branch + author identity when no keyword
- **Assignee fill-in** from PR author (only when the task has no assignee)
- **Type** from branch convention / PR labels
- **QA auto-assignment** by tier (repo tier × QA tier eligibility)
- Full status state machine: Needs Assigned → Assigned → Needs QA → In QA → QA Verified / QA
  Rejected → In Progress (resume) → Paused → Needs QA again → Completed
- Per-board schema awareness (group-by-repo-name, per-board person/status field keys)
- Worker-only production mode (`BOARDMAN_ENABLE_AGENT_API=false` disables the chat/UI routes)
- 289 offline tests passing; full lifecycle verified live on `diri-cyrex`

---

## 5. What's Needed to Finish

These are **configuration and deployment** items, not new feature work.

### A. QA roster (mostly automatic)
Boardman builds the QA roster from the **`Team-Deepiri/support-team`** GitHub team and auto-matches
each member to their Plaky user (11 members load today). You only need `member_overrides` in
`team_assignments.yml` to **correct a bad match** or set a member's `qa_tier` (1/2/3). Confirm the
roster looks right before wide rollout.

### B. Production secrets & deploy (carried over from earlier)
- Generate real `GITHUB_WEBHOOK_SECRET`, `WORKER_INTERNAL_SECRET`, `ROUTE_SECRET`
- Move `PLAKY_API_KEY` to a dedicated service account (not a personal account)
- Set `BOARDMAN_ENABLE_AGENT_API=false` for the worker-only production box
- Deploy on the VPS (`docker-compose.prod.yml`) and run the preflight + smoke scripts

### C. Register the GitHub webhook
On the org/repos, point the webhook at `https://<host>/api/v1/webhooks/github` with events:
**issues, pull_request, pull_request_review, pull_request_review_comment, issue_comment**.
The `pull_request` event must include **synchronize** (it drives the resume-to-In-Progress step).

### D. Board placement (ongoing)
Boards are consolidating to ~5 category boards (Platform+Services, Bots, Developer Tools,
Creative, Miscellaneous), one group per repo. As each repo gets its group, set its
`plaky_board_id` in `repos.yml` — the group is then matched automatically by name. Repos not yet
placed route to the main board.

### E. Optional: inference booster
A free-tier Gemini key can sharpen identity/title matching in ambiguous cases. Config-only:
`LLM_PROVIDER=gemini`, `GEMINI_API_KEY`, `ASSIGNMENT_IDENTITY_LLM_ENABLED=true`.

---

## 6. How to Run / Test It Locally

```bash
# 1. Backend (terminal 1)
poetry run python -m boardman.main          # http://localhost:8090

# 2. Worker, for queued jobs (terminal 2, optional)
poetry run python -m boardman.sqlite_worker

# 3. Drive a full simulated PR lifecycle and watch the task move
poetry run python scripts/sim_pr_lifecycle.py
```

The simulation sends signed GitHub webhooks and prints the Plaky task's Status / Assignee / QA
after each step. Run the test suite with:

```bash
poetry run pytest -m "not integration and not plaky_live and not agent_e2e_live"
```

> The repo is in a production-clean state (no test-only routing or roster entries). To re-run the
> local simulation, temporarily route a repo to a board with the QA status schema and ensure a QA
> roster is present. The go-live steps are in [GO_LIVE_CHECKLIST.md](./GO_LIVE_CHECKLIST.md).
