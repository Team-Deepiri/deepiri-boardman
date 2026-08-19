# Boardman — Production Readiness Specification

> Last verified live: 2026-08-14. Every claim in this document is backed by a named
> check that ran against the real board (Bots / 269028) — not by intent. Print to PDF
> from any markdown viewer.

## 1. What Boardman is

The lifecycle implementation is event-source-of-truth: webhook and reconciliation paths
resolve the same canonical GitHub state, apply only changed Plaky fields, and record
activity markers in `SyncLog`. Production should acknowledge verified webhooks through the
SQLite worker (`GITHUB_WEBHOOK_ASYNC_ENABLED=true`) and keep the bounded reconciliation loop
available for delivery gaps. The live claims below are historical operator evidence; rerun
the matrix against the current board before a new deployment.

A FastAPI service (:8090) + React UI (:5176) that keeps GitHub and Plaky telling the
same story: issues and PRs become tasks, review activity becomes status, and an LLM
assistant (gpt-5.1, LangChain tool-calling) reads repos and manages the board on
request. SQLite for sessions/links/dedupe; webhooks in production, a polling loop
(`TESTING_LIVE_PLAKY=true`, 15s) as the local stand-in.

## 2. The state machine

Verified live three ways: `plaky_automation_matrix.py` (33/33 transitions + 14/14 edge
guards), and `scripts/production_checklist.py`, which walks the employer checklist end to
end — **52 pass, 0 fail, 1 documented limitation** on 2026-08-19. That runner opens a real
GitHub issue and mutates it for real (label, priority, assignee, edit, comment, close,
reopen) with nothing syncing it by hand, then drives the PR/QA/merge legs through the real
webhook endpoint, asserting the live board after every step.


```
issue opened            -> task on Bots/deepiri-boardman: NEEDS ASSIGNED
                           (or Assigned + owner, when the GitHub issue has an assignee)
                           Type: native GitHub Type > labels > Feature default
                           Priority: inferred from text/labels
issue labeled/typed/    -> re-syncs Type and fills the assignee (fill-only, never
  assigned later           clears curated Plaky state)
sidebar Priority set    -> Priority mirrors GitHub's issue field / priority label
  or changed               (Low/Medium/High, Urgent -> VERY IMPORTANT); text-inferred
                           priority never overwrites the board after creation
issue title/body edited -> re-syncs where the board can store it; Plaky's public API
                           cannot rewrite an existing item's text (OPTIONS on an item
                           answers Allow: GET,HEAD,DELETE,OPTIONS and /fields refuses
                           `title`), so the edit is mirrored as a deduped comment on
                           the task instead of being silently dropped
issue closed            -> Completed, remembering the pre-close status
issue reopened          -> RESUMES the pre-close status; no record -> owner ? Assigned
                           : NEEDS ASSIGNED (never a blanket In Progress)
PR opened (linked)      -> assignee = PR author; QA picked + @mentioned + requested;
                           non-draft -> Needs QA; draft -> follows assignee
QA is never the author  -> the PR author is removed from the candidate pool (self
                           review). GitHub refuses a review from the PR's own author,
                           so assigning them leaves a reviewer who cannot act and the
                           QA-gated rejection path stops responding to anyone. If that
                           empties the pool, QA stays unset with a reason.
                           NOTE: an approval from ANY reviewer sets QA Verified by
                           design; only "request changes" is restricted to the
                           assigned QA.
PR opened (no match)    -> a REAL linked task is created (title/type/priority from the
                           PR, assignee = author, Needs QA, QA assigned)
PR labeled later        -> Type re-syncs (labels only; branch had its chance)
QA rejects              -> QA Rejected (only the assigned QA can)
dev pushes after reject -> back to Needs QA (resubmission)
QA comments             -> In QA (assigned QA counts even off the support roster)
approval w/ failing CI  -> HELD: task does not read QA Verified while checks are red
new commits after       -> QA Verified/Rejected invalidated -> back to Needs QA
  a verdict
approve / dismiss       -> QA Verified / back to In QA
merge (last open PR)    -> Completed        close unmerged (last PR) -> In Progress
no hardcoded QA names   -> exclusion list only (Joe, Austin, Devin, Sean, Nathan,
                           Hameeda, Andy N); optional qa_bug_specialist ships empty
```

Edge guards (all live-verified): duplicate webhook deliveries ignored (status committed
before the response — a race was caught and fixed here), re-sent issues dedupe by
mapping, second PR never re-picks QA or steals the assignee, non-assigned-QA rejections
ignored, merge completes only when the LAST PR merges, unsupported events refused,
Boardman ignores its own comments, comment mirroring survives restarts.

## 3. Latency budgets (measured)

| Path | Measured | Budget | Bottleneck |
|---|---|---|---|
| Warm assistant Q&A | 1.8s | <3s | — |
| Cold assistant Q&A | ~12–17s | <20s | model decode + cold schema fetch |
| 5-task batch create | 38–47s (was 2m13s) | <60s | Plaky server burst shaping (13–25s) + 2 model turns |
| Event → board (local poll) | ≤15s + handler (~2–5s) | <25s | poll interval; webhooks in prod are push |
| One Plaky create (wire) | ~2s / 5 calls | <5s | — |

Every agent turn logs `db+persist / context / llm+tools / total` with rolling p50/p95;
every tool call logs its wall time. Slowness is diagnosable from one log line.

## 4. Failure honesty (the design rule that matters most)

Every failure mode found this cycle was a variant of one disease: *stating something
false with confidence*. The standing protections:

- errors classify by SDK status code first — a transient 429 says "quota, resets in a
  minute" while an exhausted balance (429 insufficient_quota, 402, Anthropic's 400)
  says "billing — add credits, re-sending will not help"; never "check your API key".
  Gemini's transient throttle reuses OpenAI's billing sentence and stays rate_limited

- a provider failure can no longer eat the user's message (persisted before the LLM runs)
- caches never store failures; PR review/CI state is never cached
- partial data is always labeled (`returned/total/truncated`, `UNAVAILABLE` sections)
- the assistant must scan before judging ("does X work?" = run the tools), must retry
  failed writes in-turn with the tool's own error, and may not invent causes
- batch creates dedupe against the board ("Already on the board — not re-created")

## 5. Production deploy checklist

- [ ] `TESTING_LIVE_PLAKY=false` (poller off; org webhook delivers instead)
- [ ] Webhook registered → deployed HTTPS URL + `GITHUB_WEBHOOK_SECRET`
- [ ] PAT scopes: Issues:write + Pull requests:write (mentions/reviewer requests) and
      Contents:read. Current local PAT lacks Contents:write — fine for the service,
      it never pushes code
- [ ] `GITHUB_ORG=Team-Deepiri`; routing `repos.yml` → board 269028 / group 933385
- [ ] Secrets out of `.env` into the host store; rotate anything that touched dev
- [ ] `scripts/deploy_preflight.sh` green; `plaky_automation_matrix.py` + `--edge` green
      against the production board after first deploy
- [x] Board vocabulary is complete. Type carries Story, Task, Bug, Research, Feature
      (17) and Refactor (18) — both confirmed live on the Bots board. Status needs no
      "Needs QA Again" column: a resubmission returns to Needs QA by design

## 6. Known gaps / accepted debt

| Item | Why deferred |
|---|---|
| Deterministic fast-lane intent router (PDF plan §2) | biggest remaining speed lever; needs its own tested router — a misroute is worse than 2s |
| Plaky→GitHub reverse sync | scoped as a task on the board (7116261); loop-prevention design first |
| ~~Reconciliation~~ | DONE: POST /api/v1/reconcile/{owner}/{repo} walks current open issues/PRs through the idempotent handlers (bounded, repeat-safe) |
| Rolling history summarization | window of 16 recent messages suffices today |
| CI latency/quality gate | the p50/p95 metrics now exist to gate on |
| Renaming an existing task | Plaky API exposes no item update verb (Allow: GET/HEAD/DELETE); adding a "Details" text field to the board would at least let the body sync into a column |
| Sub-15s five-task creates | bounded by Plaky burst shaping + model decode; needs the fast lane + a faster planning model |

## 7. Operator runbook (the short version)

The previously listed synchronization gap is now closed for GitHub-to-Plaky: issue/PR
metadata, assignees, statuses, review/comment mirroring, delivery retries, identity
uniqueness, and bounded reconciliation are implemented. The remaining product gap is the
reverse Plaky-to-GitHub direction, which still needs loop-prevention semantics.

New repo → add to `repos.yml` (board/group) — everything else is schema-discovered.
New QA → GitHub support team (live roster) or `team_assignments.yml` fallback.
Policy roles (`qa_bug_specialist`, exclusions) → `team_assignments.yml`.
Wrong state on the board → check `SyncLog` (every write is recorded) and the per-turn
timing lines; replay any GitHub event through `POST /api/v1/webhooks/github` with the
real payload — every handler is idempotent.
