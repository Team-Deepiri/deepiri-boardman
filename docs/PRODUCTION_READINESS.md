# Boardman — Production Readiness Specification

> Last verified live: 2026-08-14. Every claim in this document is backed by a named
> check that ran against the real board (Bots / 269028) — not by intent. Print to PDF
> from any markdown viewer.

## 1. What Boardman is

A FastAPI service (:8090) + React UI (:5176) that keeps GitHub and Plaky telling the
same story: issues and PRs become tasks, review activity becomes status, and an LLM
assistant (gpt-5.1, LangChain tool-calling) reads repos and manages the board on
request. SQLite for sessions/links/dedupe; webhooks in production, a polling loop
(`TESTING_LIVE_PLAKY=true`, 15s) as the local stand-in.

## 2. The state machine (verified: 33/33 matrix + 14/14 edge guards, live)

```
issue opened            -> task on Bots/deepiri-boardman: NEEDS ASSIGNED
                           (or Assigned + owner, when the GitHub issue has an assignee)
                           Type: native GitHub Type > labels > Feature default
                           Priority: inferred from text/labels
issue labeled/typed/    -> re-syncs Type and fills the assignee (fill-only, never
  assigned later           clears curated Plaky state)
PR opened (linked)      -> assignee = PR author; QA picked + @mentioned + requested;
                           non-draft -> Needs QA; draft -> follows assignee
PR opened (no match)    -> a REAL linked task is created (title/type/priority from the
                           PR, assignee = author, Needs QA, QA assigned)
PR labeled later        -> Type re-syncs (labels only; branch had its chance)
QA rejects              -> QA Rejected (only the assigned QA can)
dev pushes after reject -> Needs QA Again (resubmission)
QA comments             -> In QA (assigned QA counts even off the support roster)
approval w/ failing CI  -> HELD: task does not read QA Verified while checks are red
new commits after       -> QA Verified/Rejected invalidated -> Needs QA Again
  a verdict
approve / dismiss       -> QA Verified / back to In QA
merge (last open PR)    -> Completed        close unmerged (last PR) -> In Progress
issue closed / reopened -> Completed / In Progress
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
- [ ] Ask the Plaky admin to add board options: Status "Needs QA Again", Type "Feature"
      and "Refactor" (handlers degrade gracefully until then: NQA-Again→Needs QA,
      Feature→Story)

## 6. Known gaps / accepted debt

| Item | Why deferred |
|---|---|
| Deterministic fast-lane intent router (PDF plan §2) | biggest remaining speed lever; needs its own tested router — a misroute is worse than 2s |
| Plaky→GitHub reverse sync | scoped as a task on the board (7116261); loop-prevention design first |
| ~~Reconciliation~~ | DONE: POST /api/v1/reconcile/{owner}/{repo} walks current open issues/PRs through the idempotent handlers (bounded, repeat-safe) |
| Rolling history summarization | window of 16 recent messages suffices today |
| CI latency/quality gate | the p50/p95 metrics now exist to gate on |
| Bots board vocab gaps | Plaky-admin action, see checklist |
| Sub-15s five-task creates | bounded by Plaky burst shaping + model decode; needs the fast lane + a faster planning model |

## 7. Operator runbook (the short version)

New repo → add to `repos.yml` (board/group) — everything else is schema-discovered.
New QA → GitHub support team (live roster) or `team_assignments.yml` fallback.
Policy roles (`qa_bug_specialist`, exclusions) → `team_assignments.yml`.
Wrong state on the board → check `SyncLog` (every write is recorded) and the per-turn
timing lines; replay any GitHub event through `POST /api/v1/webhooks/github` with the
real payload — every handler is idempotent.
