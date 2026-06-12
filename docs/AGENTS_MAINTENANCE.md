# Documentation maintenance for coding agents

Coding agents and contributors must keep project docs aligned with the repo. **Update docs in the same PR** as code changes when triggers below apply.

## Doc roles

| Doc | Audience | Agent must update when… |
|-----|----------|----------------------|
| [AGENTS.md](../AGENTS.md) | Coding agents | Structure, CLI/API, env vars, implemented vs planned (see triggers below) |
| [PLAN.md](PLAN.md) | Source of truth | Major feature ships, phase status changes, architecture shifts |
| [AGENT_PLAN.md](AGENT_PLAN.md) | Agent module detail | Agent tools, prompts, memory, runner, or Plaky tool wrappers change |
| [NEW_FEATURES_PLAN.md](NEW_FEATURES_PLAN.md) | Future features | Planned item ships (full or partial) — update status column |
| [README.md](../README.md) | Humans + agents | User-facing CLI/API/deploy commands change |
| [SETUP.md](../SETUP.md) | Onboarding | Credentials, webhook events, install steps change |
| [DIRECTION.md](../DIRECTION.md) | This repo's goals | This repo's phase, scope, or priorities change |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Ops | Compose services, secrets, deploy scripts change |

Index only in AGENTS.md (link, don't duplicate): `DEPLOYMENT.md`, `BOARDMAN_*.md`, `ADDITIONAL_FEATURES.md`, `TAKEHOME_STATUS.md`, templates.

---

## AGENTS.md triggers

| Trigger | Sections to update |
|---------|-------------------|
| New/removed top-level package or major `boardman/` subdirectory | Repo map, Key modules |
| New CLI subcommand | CLI surface |
| New API route under `/api/v1/` | API surface |
| New required env var in `settings.py` or `.env.example` | Configuration |
| New markdown doc in `docs/` or root | Documentation index |
| Feature moves from planned → implemented | Implemented vs planned, Key modules |
| Docker service/port/command change | Run / test / deploy, Architecture |
| New Alembic table agents should know | DB tables, Key modules |
| Plaky routing table/category change | Plaky board routing |

## PLAN.md triggers

- Implementation phase moves to **Done** or **Partial**
- New major subsystem (e.g. new integration)
- CLI/API surface changes described in plan body
- Verification checklist item satisfied
- "Planned" / "to build" language becomes inaccurate

## AGENT_PLAN.md triggers

- New/renamed file under `boardman/agent/` or `boardman/agent/tools/`
- CLI or API agent endpoints added/removed
- Plaky client methods used by agent tools change
- System prompt location or `PROMPT_VERSION` contract changes

## NEW_FEATURES_PLAN.md triggers

- Any §1–§7 item ships (update status: Done / Partial / Superseded)
- New planned feature proposed (add row to status table)

## README.md / SETUP.md triggers

- New documented CLI command or API endpoint for users
- Credential or webhook requirement changes
- Default ports or Docker compose file names change

## DIRECTION.md triggers

- This repo's current phase or priorities shift
- Major milestone completed or new focus area

---

## Per-update checklist

1. Edit only affected sections (do not rewrite whole files).
2. Refresh **Documentation index** in AGENTS.md if any `*.md` was added or removed.
3. Move items from **Planned** → **Implemented** in AGENTS.md and PLAN.md when shipped.
4. Update status tables in NEW_FEATURES_PLAN.md for partial/full delivery.
5. Set **Last verified** date in AGENTS.md.
6. Add a row to **Document history** in PLAN.md for substantial doc reconciliations.
7. Run verification:

```bash
poetry run python scripts/verify_agents_md.py
```

## What not to duplicate

- Full architecture prose → [PLAN.md](PLAN.md)
- Deploy runbook → [DEPLOYMENT.md](DEPLOYMENT.md)
- Agent module detail → [AGENT_PLAN.md](AGENT_PLAN.md)
- Future specs → [NEW_FEATURES_PLAN.md](NEW_FEATURES_PLAN.md), [ADDITIONAL_FEATURES.md](ADDITIONAL_FEATURES.md)

AGENTS.md indexes and summarizes; other docs hold depth.

## Staleness review

- On release changing CLI/API
- When PLAN.md phases change status
- Quarterly spot-check; bump AGENTS.md **Last verified**

Verify script warns if **Last verified** is older than 90 days.

## Cursor rule

`.cursor/rules/agents-md-maintenance.mdc` reminds agents editing `boardman/`, `docs/`, or root config files to check this document.

## Product agent alignment

`boardman/agent/tools/repo_tools.py` should include `AGENTS.md`, `README.md`, `DIRECTION.md`, and `docs/PLAN.md` in local scan paths so the Board Manager agent and coding agents share context.
