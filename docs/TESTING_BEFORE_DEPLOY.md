# Testing the Plaky automation before deploy

Three layers, cheapest first. Run them in order; each one catches what the layer below
cannot.

| Layer | Command | Time | Touches |
|---|---|---|---|
| 1. Unit suite | `poetry run pytest -q` | ~2 min | nothing external |
| 2. Automation matrix | `poetry run python scripts/plaky_automation_matrix.py --loop 3` | ~4 min | real Plaky board (creates + deletes a task) |
| 3. Edge guards | `poetry run python scripts/plaky_automation_matrix.py --edge` | ~2 min | real Plaky board |
| 4. Live on GitHub | by hand, see below | ~10 min | real repo + real board |

Layers 2 and 3 need the backend running (`poetry run python -m boardman.main`) because
they POST to the real webhook endpoint at `localhost:8090`.

---

## Layer 1 — unit suite

```bash
poetry run pytest -q
poetry run ruff check boardman tests && poetry run black --check boardman tests
```

Covers handler logic with no network: status resolution, QA scoring, priority inference,
type mapping, dedupe, the roster exclusion list.

## Layer 2 — the automation matrix

```bash
poetry run python scripts/plaky_automation_matrix.py --loop 3
```

Drives **every documented transition** through the real `POST /api/v1/webhooks/github`
endpoint against the real board, and reads the Plaky item back after each one. It asserts
the resulting **state**, not merely that a handler returned `ok` — a handler can return
success and write nothing.

Synthetic issue/PR numbers are used, so no real GitHub artifact is touched. The Plaky item
is deleted at the end unless you pass `--keep`.

What it asserts, in order:

| # | Trigger | Required Plaky state |
|---|---|---|
| 1–4 | issue opened | `NEEDS ASSIGNED`, Type from label, Priority inferred, **no QA yet** |
| 5 | comment on the issue | mirrored onto the task |
| 6–9 | PR opened as **draft** | dev assigned, QA assigned, **not** `Needs QA`, QA is not an excluded lead |
| 10+ | draft → ready for review | `Needs QA` |
| | ready → back to draft | `In Progress` |
| | reviewer approves | `QA Verified` |
| | assigned QA requests changes | `QA Rejected` |
| | work resumes after rejection | `Needs QA Again` |
| | support-team member comments | `In QA` |
| | PR merged | `Completed` |
| | PR closed unmerged | `In Progress` |
| | issue closed / reopened | `Completed` / reopened |

`--loop 3` runs the whole matrix three times with different synthetic ids. Run it more than
once: it catches ordering flakiness and Plaky's read-after-write lag, which a single pass
can hide.

## Layer 3 — edge guards

```bash
poetry run python scripts/plaky_automation_matrix.py --edge
```

These assert the automation **does not** act when it must not — the failures that put a
confident wrong state on the board:

| # | Guard |
|---|---|
| E1 | a duplicate webhook delivery does not create a second task |
| E2 | the same issue re-sent with a new delivery id still does not duplicate |
| E3 | merging the first of two PRs on one task does **not** complete it |
| E4 | a "request changes" from someone who is not the assigned QA is ignored |
| E5 | merging the last open PR completes the task |
| E6 | an unsupported event type is refused, not silently accepted |
| E7 | Boardman ignores its **own** comments (it posts as a human PAT, not a `[bot]`) |
| E8 | a restart replaying the catch-up window does not re-post a mirrored comment |

## Layer 4 — live on the real repo

With `TESTING_LIVE_PLAKY=true` and the backend running, the poller replays real GitHub
activity through the same handlers a production webhook hits. Do this once before deploy:

1. **Open an issue** → task appears in ~60s: `NEEDS ASSIGNED`, Type + Priority inferred,
   Assignee and QA both empty.
2. **Comment on it** → mirrored onto the task.
3. **Push a commit** whose message says `Fixes #N`, on a branch **with an open PR** →
   commented onto the task with author and link.
4. **Open the PR** → QA assigned: @-mentioned on GitHub, requested as reviewer, written
   into the QA field on the task.
5. **Approve / request changes / merge** → `QA Verified` / `QA Rejected` / `Completed`.

Commits are polled on the default branch **and the head branch of every open PR**. A commit
on a branch with no PR is not seen — that is intended, not a bug.

---

## Before you flip it to production

- [ ] `TESTING_LIVE_PLAKY=false` — the poller must not run in production; the registered
      GitHub webhook delivers the same events instead.
- [ ] Webhook registered at the org or repo, pointing at the deployed HTTPS URL, with a
      shared secret set in `GITHUB_WEBHOOK_SECRET`.
- [ ] `GITHUB_PAT` has **Issues: write** and **Pull requests: write** — without them QA is
      still assigned in Plaky, but the GitHub @mention and reviewer request are skipped.
      Boardman logs one clear hint rather than failing silently.
- [ ] Secrets rotated out of `.env` and into the host's secret store.
- [ ] `scripts/deploy_preflight.sh` passes.
- [ ] `GITHUB_ORG=Team-Deepiri` (not `deepiri-org` — that org does not exist and every call
      404s).

## Reading a failure

The matrix prints one line per assertion with the value it actually read:

```
  PASS  12 PR ready for review -> Needs QA        = Needs QA
  FAIL  13 approve -> QA Verified                 = In QA
```

A `FAIL` shows the state the board was really in, so the diagnosis starts from evidence.
If a check fails only sometimes, it is usually Plaky's read-after-write lag — the script
already retries, but a slow board can outlast it. Re-run before treating it as a defect.
