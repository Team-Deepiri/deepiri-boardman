# Team-Deepiri/deepiri-boardman Direction

## What This Repo Does

GitHub ↔ Plaky automation service: webhooks sync issues/PRs to Plaky tasks, `repos.yml` routes tasks to the right board area, and an AI layer (`boardman scan`, LangChain agent) generates and manages work from per-repo `DIRECTION.md` files. Includes FastAPI API, Typer CLI, SQLite persistence, optional Ollama, and `boardman-ui` chat.

## Current Phase

Core sync, scan, agent, UI, and production Docker stack are shipped. Focus: production deploy hardening, QA assignment tuning, and features in [docs/NEW_FEATURES_PLAN.md](docs/NEW_FEATURES_PLAN.md) (bidirectional Plaky→GitHub, assignee automation).

## What Needs to Be Done

- [ ] Complete production `.env` + `repos.yml` / `team_assignments.yml` on target host
- [ ] Plaky → GitHub bidirectional sync ([NEW_FEATURES_PLAN.md](docs/NEW_FEATURES_PLAN.md) §1)
- [ ] Phase C automated weekly scan + norozo summary ([PLAN.md](docs/PLAN.md))
- [ ] QA tier distribution tuning per [TAKEHOME_STATUS.md](docs/TAKEHOME_STATUS.md)
- [ ] Keep [AGENTS.md](AGENTS.md), [PLAN.md](docs/PLAN.md), and related docs in sync with code changes

- [ ] ClickUp as a second task provider ([docs/CLICKUP.md](docs/CLICKUP.md)): code complete for task routes, issue/PR/review webhook sync, QA assignment, agent tools, discovery and planning, and covered by mocked tests plus an HTTP-level lifecycle trace (`tests/test_clickup_lifecycle_trace.py`). **Not yet validated against a real ClickUp workspace or the deployed boardman.deepiri.com**: run `scripts/clickup_live_trace.py` / [docs/CLICKUP_LIVE_CHECKLIST.md](docs/CLICKUP_LIVE_CHECKLIST.md) once a token exists

## What's NOT In Scope

- Discord notifications (norozo)
- Kanban providers other than Plaky and ClickUp (e.g. Linear) — see [ADDITIONAL_FEATURES.md](docs/ADDITIONAL_FEATURES.md)
- Running Ollama in cloud production
