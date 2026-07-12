# PR #63 — Meeting Plans + Deepiri Huddle Integration (evidence)

> Retrospective record of what this PR actually changed in the repo. The
> forward-looking design lives in [MEETING_PLANS_PLAN.md](MEETING_PLANS_PLAN.md);
> this document is the delivered-work evidence for review and future archaeology.

- **Branch:** `connor/feat/planning-meeting-plans` → **base:** `dev`
- **PR:** [#63](https://github.com/Team-Deepiri/deepiri-boardman/pull/63)
- **Scope:** 52 files changed (~4.7k insertions). New `boardman/planning/` feature
  ported from `deepiri-huddle`, exposed via CLI, REST, and the agent, plus a
  security fix, review-driven cleanups, and a provenance-clear reorg.

---

## 1. What the PR delivers

Meeting-plan generation is now a first-class boardman feature: given a team and
meeting type, it assembles live organizational context (GitHub PRs, Plaky board
items, boardman sync state, `DIRECTION.md`/scan history), prompts an LLM, and
renders a facilitator-ready markdown plan — optionally written to disk.

**Surfaces**

| Surface | Entry point |
|---------|-------------|
| REST | `POST /api/v1/plans/generate` (`boardman/routes/plans.py`) |
| CLI | `boardman plan …` (`boardman/cli/plan_commands.py`) |
| Agent tool | `generate_meeting_plan` (`boardman/agent/tools/planning_tools.py`) |
| Readiness | plan doctor checks in `boardman/readiness.py` |

**Engine** (`boardman/planning/`): `MeetingPlanner` composes four context
providers through `ContextAggregator`, calls the provider-agnostic
`BoardmanPlanningLlm`, and validates output structure with
`validate_meeting_plan_markdown`.

---

## 2. Huddle provenance is now explicit (file organization + naming)

The planning engine was ported wholesale from the standalone `deepiri-huddle`
project. To keep it obvious what was *taken from huddle* versus written natively
for boardman, the ported modules live in a dedicated subpackage:

```
boardman/planning/
  service.py            # boardman-native: meeting-plans service (REST/CLI/agent)
  context_aggregator.py # boardman-native: orchestrates the huddle providers
  team_config.py        # boardman-native: centralized team-config resolution
  team_models.py        # boardman-native
  huddle/               # ← ported from deepiri-huddle (Wave 1 integration)
    __init__.py         #   provenance/boundary docstring
    ORIGINS.md          #   file-by-file map back to huddle sources
    async_bridge.py     #   sync/async bridge (added by the review fixes)
    context_direction.py  context_github.py  context_plaky.py  context_sync.py
    planner.py  plan_output.py  llm_adapter.py  models.py  schedule.py
    team_repos.py  team_plaky_boards.py
```

The 11 ported modules are pure `git mv` renames (no logic change); only import
paths were repointed across routes, CLI, agent tools, `repos_config.py`, and
tests. Boardman's own layer imports *down* into `planning.huddle.*`, making the
"boardman built on the ported huddle engine" relationship visible in the imports.
See [`boardman/planning/huddle/ORIGINS.md`](../boardman/planning/huddle/ORIGINS.md)
for the file-by-file mapping.

---

## 3. Security fix — path traversal in plan output (CodeQL `py/path-injection`)

A user-supplied `output_path` (REST body / CLI `--output`) flowed unchecked into
`mkdir` + `write_text`, allowing plan markdown to be written to an arbitrary
location. Every writer is now confined to `settings.planning_output_dir`.

**Why the first attempts still failed CodeQL, and the final form that passed:**

| Attempt | Approach | CodeQL result |
|---------|----------|---------------|
| 1 | `os.path.realpath` + `startswith` | ❌ `realpath` is itself a path-injection sink |
| 2 | `Path.resolve()` + `.relative_to()` | ❌ `.resolve()` is a sink; `.relative_to()` not recognized as a barrier |
| 3 | `abspath`/`normpath` + **compound** guard | ❌ `target != base and not startswith` left a branch CodeQL couldn't prove |
| 4 (final) | `abspath`/`normpath` + **single** `startswith(base + os.sep)` guard | ✅ recognized `PathNormalization` + `SafeAccessCheck` barrier |

Confinement is enforced at the sink in `service.confine_to_output_dir` (covers
REST + CLI); the REST route additionally rejects escapes with a `422`. Behavior:
absolute/`..` paths outside the output dir are rejected, subdirectories allowed.
Symlink resolution was intentionally dropped (it was the flagged operation);
`..`/absolute traversal defense is unchanged. **Result: CodeQL "No new alerts."**

Tests: `test_plans_generate_route_rejects_path_traversal`,
`test_plans_generate_route_confines_output_path`,
`test_generate_plan_rejects_output_path_traversal`.

---

## 4. Review-driven cleanups

### 4.1 `asyncio.run()` in synchronous methods → shared `run_sync` bridge
The context providers and LLM adapter exposed a sync API but did async I/O via
bare `asyncio.run()`, which raises `RuntimeError: asyncio.run() cannot be called
from a running event loop` if reached from a running loop, and rebuilt an event
loop per call. Introduced `boardman/planning/huddle/async_bridge.py::run_sync`,
which uses `asyncio.run()` when no loop is running and offloads to a worker-thread
loop when one is — making the sync API safe from any context and de-duplicating
the pattern. Applied in `context_direction.py`, `context_sync.py`,
`context_plaky.py`, `llm_adapter.py`. Guarded by `tests/test_planning_async_bridge.py`.

### 4.2 Removed the `_ClientCtx` wrapper in `context_github.py`
The hand-rolled context-manager wrapper (whose real purpose was to *not* close an
injected/borrowed `httpx.Client`) was replaced with the standard
`contextlib.nullcontext(self._client)` — same borrow-without-closing semantics,
no custom class.

> Deferred (noted, not changed): the fully "async-first, sync wrapper at the
> service layer" rewrite the review suggested is a larger refactor; `run_sync`
> resolves the correctness/robustness defect without rippling signatures through
> `planner`/`aggregator`/`service`.

---

## 5. Test & CI evidence

- **Full suite:** `364 passed, 19 skipped` locally.
- **CI on head:** `Analyze (python, javascript-typescript)` ✅, **`CodeQL` ✅
  ("No new alerts")**, `Python (Poetry) 3.11` ✅, `3.12` ✅, Docker/Node ✅.
- New/updated tests: `test_plans_route.py`, `test_planning_service.py`,
  `test_planning_async_bridge.py`, plus the ported `test_planning_*` suites.
- Offline acceptance: `scripts/acceptance_offline.sh`, `scripts/acceptance_plan.sh`
  (CI-green without live keys via `tests/fixtures/planning/*.json`).

---

## 6. Commit trail

| Commit | Summary |
|--------|---------|
| `6fef3d5` | Wave 1 huddle integration (ported planning engine) |
| `3319ffe` | Wire aggregator into `MeetingPlanner` prompt |
| `de45722` / `2911028` | Waves 4–5: surfaces (API/agent/readiness) + acceptance |
| `e913546` | Centralize team config resolution + plan doctor (wave 6) |
| `d7a2b6e` → `7a4eb5c` | Path-traversal fix, iterated to a CodeQL-recognized barrier |
| `1e789d5` | Segment ported huddle logic into `planning/huddle/` subpackage |
| _(this change)_ | `run_sync` async bridge, drop `_ClientCtx`, this evidence doc |
