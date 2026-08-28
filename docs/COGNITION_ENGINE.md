# Cognition Engine

How Boardman separates fact from inference, intent from reality, and surfaces contradictions.

> Status: implemented. Evidence model, intent-vs-reality verdicts, contradiction detection,
> and evidence-backed planning are live. The golden tests pin the verdict wiring.

## The problem this solves

A stale doc and a working implementation look the same to Boardman: both are just text in
the prompt. The cognition engine gives Boardman the distinction between what it *knows for
a fact*, what it *infers*, what the docs *intend*, and what actually *happens* -- and says
when those disagree.

## What's built

| Component | What it does | Where |
|---|---|---|
| **Evidence model** | Typed `Evidence` value object with `kind` (fact/inference/intent/observed), source provenance, and timestamps | `boardman/cognition/evidence.py` |
| **BehaviorSpec registry** | Seeded behaviors with `expected_present` entries for deterministic file/function existence checks | `boardman/cognition/behaviors.py` |
| **Intent-vs-reality engine** | `compare_intent_to_reality(spec)` returns ALIGNED/PARTIAL/BROKEN/UNKNOWN without an LLM call | `boardman/cognition/intent_reality.py` |
| **Contradiction detection** | The existing reconciliation loop emits contradictions when it repairs GitHub-Plaky drift | `boardman/services/reconcile.py` (extension) |
| **Evidence-backed planning** | `planning_candidates` tool deduplicates proposed tasks against open work and ranks by evidence | `boardman/cognition/planning.py`, `boardman/agent/tools/cognition_tools.py` |
| **Cognition rendering** | `render_cognition_block()` puts verdicts and contradictions into the prompt block the LLM sees | `boardman/cognition/rendering.py` |
| **Cognition storage** | Lives inside `ProjectContext.context_json["cognition"]`, no new database table | `boardman/agent/repo_context.py` (extension) |

## How it stays honest

**Verdicts are deterministic.** `compare_intent_to_reality` checks file/function existence,
not an LLM's opinion. A renamed function flips the verdict from ALIGNED to BROKEN immediately.

**Contradictions auto-resolve.** When the reconciliation loop runs and finds no drift for a
repo, any previously-recorded contradictions for that repo are removed.

**Staleness is tracked.** The `cognition_state` field mirrors `Briefing.state`: `"fresh"` when
computed, `"stale"` when the repo's `pushed_at` has moved, `"unavailable"` when never computed.

**No new infrastructure.** All cognition state lives in `context_json["cognition"]`. No new
database tables, no new Alembic migrations, no new job queues.

## Measurement

- `cognition.verdicts.{aligned,partial,broken,unknown}` -- bumped at the decision point
- `cognition.contradictions.{detected,resolved}` -- bumped per contradiction
- `scripts/benchmark_brain.py --label after --compare before` with the `audit_behavior` scenario

## What was deliberately not built

- **No vector database.** Evidence is typed provenance, not embeddings.
- **No graph database.** The relationship is spec -> verdict, stored as JSON.
- **No new job queue.** Verdicts compute in the existing knowledge sweep.
- **No new database table.** Everything lives in `context_json`.
- **No LLM-computed verdicts.** File/function existence is deterministic.
- **No fast-path routing for judgment questions.** The fast path stays for factual lookups.
- **No SOURCE_AUTHORITY module.** Source trust is implicit in the `source_type` field.

## Where to look

| File | What it does |
|---|---|
| `boardman/cognition/__init__.py` | Package marker |
| `boardman/cognition/evidence.py` | `Evidence`, `BehaviorSpec`, serialization |
| `boardman/cognition/behaviors.py` | Seeded `BEHAVIORS` registry |
| `boardman/cognition/intent_reality.py` | `compare_intent_to_reality`, `compute_cognition_verdicts` |
| `boardman/cognition/rendering.py` | `render_cognition_block` |
| `boardman/cognition/planning.py` | `PlannedTask`, `dedupe_against_existing_work`, `rank_candidates` |
| `boardman/agent/tools/cognition_tools.py` | `planning_candidates` tool |
| `boardman/agent/repo_context.py` | `save_cognition_state`, `load_cognition_state` |
| `boardman/services/reconcile.py` | Contradiction emission (extension) |
| `boardman/services/repo_knowledge.py` | Staleness marking, verdict computation trigger |
| `tests/test_cognition_golden.py` | Golden tests that pin verdict wiring |
