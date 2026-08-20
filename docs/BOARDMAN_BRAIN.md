# Boardman Brain

How the assistant knows things, and why it stopped asking GitHub for facts it already had.

> Status: implemented. Every number here was measured with `scripts/benchmark_brain.py`
> against a running instance; none of it is estimated.

## The problem this solves

Asking "what is boardman?" used to cost **eight GitHub API calls and 41,554 characters of
context**. Not because the answer was hard, but because the answer lived in three places
that never spoke to each other, so the model was handed instructions for finding it rather
than the thing itself.

The three places all existed already:

- `repos.yml` knew the repo's board and group. Instantly. For free.
- `project_contexts.context_json` held a full repo briefing, persisted across restarts,
  stamped with the `pushed_at` it was built from.
- `issue_task_map`, `pr_task_links` and `sync_log` held the live state of every issue and
  PR, written by the sync engine on every webhook — **and read back by nothing.**

## The layers

| Layer | What it answers | Where it comes from | Cost | LLM? |
|---|---|---|---|---|
| **L0 identity** | which repo, board, group, table, default branch | `repos.yml` via `resolve_identity()` | none, pure function | no |
| **L1 briefing** | purpose, structure, important paths, DIRECTION, README | `ProjectContext.context_json` | one indexed row | only to build it |
| **L2 live state** | issues on the board, PRs linked to tasks, what the sync just did | `issue_task_map`, `pr_task_links`, `sync_log` | four indexed selects | no |
| **L3 deep retrieval** | a specific file, a diff, a review verdict, CI | the existing GitHub tools | a real API call | yes, when reasoning is needed |

`boardman/agent/brain.py` composes L0–L2 into a `ProjectState`. **It makes no network
calls**, and there is a test that fails if it ever does
(`tests/test_brain.py::test_assembly_makes_no_network_call`). That property is the whole
design: the moment assembling context costs a request, it stops being context and becomes
another fetch on the critical path.

L3 is deliberately unchanged. A question that genuinely needs a file, a diff or a review
verdict still goes and gets it.

## How it stays honest

Cached knowledge is only safe to serve if it goes stale on time.

**Events invalidate immediately.** `boardman/github/change_signal.py` drops a repo's cached
reads when GitHub says the repo moved. It is wired into **both** dispatch paths — the
webhook route and the poller — because those are two independent copies of the same
dispatch table that already differ from each other, and a hook in only one of them would
leave the live poller session serving day-old context while the tests looked green.

Every hook runs **after** the sync commits, touches nothing but process-local dicts, and
swallows its own errors. A cache bookkeeping failure must never fail a sync write that
already succeeded.

**Stale is served, then repaired.** A stale briefing answers the current question
immediately and queues one refresh behind the reply
(`brain.schedule_revalidation`). Once per repo, not once per turn — a five-minute cooldown
stops a stale repo from queueing a refresh on every message.

**The sweep is a net, not a crawler.** `boardman/services/repo_knowledge.py` runs in the
worker and costs **one cheap metadata call per repo per cycle**. It compares GitHub's
`pushed_at` against the revision stored on the snapshot and only refetches a repo that
actually moved. A quiet ten minutes costs almost nothing, which is the point: a sweep that
re-reads everything would spend the API budget it exists to protect.

**Partial writers merge.** Two writers share the `ProjectContext` row. The repo scan knows
about DIRECTION, commits, issues and Plaky tasks; it knows nothing about the tree, the
README or the code signals. It used to write a stub for those anyway and stamp a fresh
timestamp, so for fifteen minutes the assistant's default context said
`Default branch: unknown` about a repo it had fully read an hour earlier.
`merge_planning_snapshot` now keeps what a partial writer does not know.

## The deterministic router

`boardman/agent/fast_path.py` answers questions whose entire answer is already in memory,
with **no LLM call and no network request**:

- what the default branch is
- how many issues from this repo are on the board, and which numbers
- how many PRs are linked to a live task, and how many merged
- which Plaky task an issue number maps to
- which board and group the repo routes to
- what repo the session is working with

What it deliberately does **not** answer:

- anything containing "right now", "currently", "check GitHub", "latest" — those are
  requests to go and look, and answering them from a mapping table is exactly the
  stale-answer failure the design is meant to prevent
- anything asking for a write
- anything needing judgment, ranking, or code

Missing an intent costs one LLM call. Claiming one wrongly produces an instant, confident,
wrong answer with no tool call to correct it, so `tests/test_intent_router.py` has as many
must-fall-through cases as must-answer cases.

## Measurement

None of this was measurable before, so:

- `boardman/observability/counters.py` — LLM calls, external API calls, tool calls, cache
  hit/miss per cache, context size per turn.
- `GET /api/v1/metrics` — read-only, no payloads, no ids. The benchmark diffs two snapshots
  around each request, so a latency change can always be traced to a call that was or was
  not made.
- `scripts/benchmark_brain.py` — the scenario matrix, p50/p95, cold cache, and concurrent
  identical callers.

External calls are counted at the shared-client seam in `boardman/github/http.py`, which is
the only place both pools are created. Call sites that build their own `httpx.AsyncClient`
are **not** counted, so any "we cut API calls by N" claim is a lower bound.

## Measured effect

Same prompts, same machine, previous commit versus this one:

| scenario | GitHub+Plaky calls | LLM calls | context chars | total p50 |
|---|---|---|---|---|
| what is boardman? | 8 → **1** | 2 → **1** | 41,554 → **31,365** | 4.04 → 4.89s |
| what is deepiri-axiom? | 8 → 8 \* | 2 → 2 | 44,695 → **31,374** | 5.81 → 5.91s |
| open PRs right now | 1 → 1 | 2 → 2 | 34,159 → **31,402** | 3.73 → 4.13s |
| five most important things | 0 → 0 | 1 → 1 | 33,080 → **31,405** | 7.77 → **7.46s** |
| how does QA assignment work | 0 → 0 | 2 → 2 | 33,492 → **31,403** | 12.70 → **10.28s** |
| 4 identical callers | 0 → 0 | 8 → 8 | 44,481 → **31,372** | 7.53 → **5.07s** wall |

\* axiom fetches when its briefing is absent from the database under test, and answers
from the Brain with **0** GitHub calls when it is present. Both are correct; which one you
measure depends on whether that repo has been asked about before.

Context is now bounded at roughly 31.4k characters whatever the question, because the
Brain render is capped, where it previously ranged from 33k to 44.7k with the repo.

No scenario regressed past the benchmark's 25% budget.

One did earlier: "open PRs right now" sat at +31% until the blocking roster load that
`/sorge` flagged was fixed, which brought it back inside budget. The server's own phase
timing is the reason to trust these numbers rather than the wall clock alone: on every
turn `db+persist` is 0.02-0.10s and context assembly is 0.00-0.08s, while `llm+tools` is
5-12s. `get_project_state` itself measures 9.8ms p50. The assistant-side cost of
everything described here is under a tenth of a second; the spread belongs to the model.

## What was deliberately not built

- **No vector database.** Nothing here needs similarity search; the questions are lookups.
- **No second job queue.** The sweep is a loop inside the existing SQLite worker, and the
  refresh is a sixth entry in the existing `JOB_HANDLERS`.
- **No second context store.** `ProjectContext` already existed, already persisted, already
  carried a freshness marker. A new table would have been the mistake.
- **No changes to the sync engine.** `sync_state.py`, `resolve_plaky_status_patch`, the
  webhook dedupe layer, `diff_only`, and the status ordering rules were not touched.

## Where to look

| File | Responsibility |
|---|---|
| `boardman/agent/brain.py` | `ProjectState`, `get_project_state`, `render_project_state`, `schedule_revalidation` |
| `boardman/agent/fast_path.py` | the deterministic router |
| `boardman/agent/repo_context.py` | the L1 store and `merge_planning_snapshot` |
| `boardman/github/change_signal.py` | event-driven invalidation, both dispatch paths |
| `boardman/github/read_cache.py` | `invalidate_repo`, per-repo purge across all key namespaces |
| `boardman/services/repo_knowledge.py` | the revision-gated sweep |
| `boardman/observability/` | counters and the LangChain call counter |
| `scripts/benchmark_brain.py` | the scenario matrix |

## Review findings, and what stops them recurring

Two review passes ran against this work: a five-lens adversarial review before the push
(43 claims, 27 confirmed after verification, 16 refuted) and `/sorge` on PR #88 afterwards.
Everything acted on has a test, because a note in a changelog does not stop a regression.

### From `/sorge` on PR #88 (7.2/10)

| Finding | Verdict | Prevention |
|---|---|---|
| Two caching layers for the same tools: `_tools_cache` and `_timed_tools_cache` | **Real.** Two places to get the key wrong | One cache keyed on `(allow_writes, timed)`. `tests/test_agent_guardrails.py` asserts `runner._timed_tools_cache` no longer exists, that all four keys appear, and that a read-only turn can never see a write tool |
| `PullRequestTaskLink` has no `UniqueConstraint` so `upsert_pr_task_link` cannot upsert | **Refuted.** `UniqueConstraint("github_repo","github_pr_number","github_issue_number")` is in `models.py:36` and in the live SQLite schema. Sorge also proposed a different key; the existing one is correct, because the table is one row per (repo, PR, issue) | n/a |
| `load_team_assignments` still blocks the event loop on a paginated Plaky call | **Real**, and nineteen async call sites reach it | Expired-but-present memo on an event loop now serves the previous roster and rebuilds on a worker thread. Cold still blocks, deliberately. `tests/test_roster_never_blocks_the_loop.py` pins the stale serve, the single-rebuild guard, failure behaviour, and that an edited yaml still takes effect at once |
| Inconsistent return shape for a missing `GITHUB_PAT` across GitHub modules | **Real but not acted on.** Cross-cutting and pre-existing; changing return types across those modules is a bigger change than the confusion it removes, and no caller currently mishandles it | Noted here |
| Bare `except Exception` around the QA profile disk cache | **Real but not acted on.** Deliberate: a corrupt cache file must never take the process down, and the narrower set would have to cover `OSError`, `JSONDecodeError`, `UnicodeDecodeError` and pickle-ish failures | Noted here |

### From the earlier `/sorge` pass (7.8/10)

| Finding | Prevention |
|---|---|
| The org repo listing is cached for ten minutes with no invalidation, so a new repo is invisible until the TTL lapses | `repository` / `create` / `delete` webhook events now clear it. `tests/test_change_signal.py` covers all three and asserts an ordinary issue event leaves it alone |
| `_tools_cache` behaviour undocumented for dynamic tool changes | Documented at the cache, and now enforced by the guardrail test above |
| `ALTER TABLE` in `session.py` bypasses Alembic | Pre-existing, outside this work, left alone |

### From the five-lens adversarial review

The confirmed findings collapsed to roughly eighteen root causes. The ones that mattered
were all the same failure: an instant confident falsehood with no tool call to correct it.

- PR counts counted link rows, so a PR closing three issues read as three pull requests
- Six of thirteen sync-log action names matched nothing any handler writes, so a busy repo
  rendered as one where nothing had happened
- The router answered "what branch should I cut this fix from" with the default branch,
  "how many open issues" from a table that keeps closed ones forever, read the 3 in "which
  task is 3 days old" as an issue number, and answered questions about one repo from
  another repo's state
- The benchmark scored an `error` SSE frame as a fast successful run, so an outage would
  have reported as the best result it had ever produced

Each has both the positive and the negative case pinned in `tests/test_intent_router.py`,
`tests/test_brain.py` and `tests/test_read_cache_invalidation.py`.
