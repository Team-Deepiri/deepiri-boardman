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

## What's NOT In Scope

- Discord notifications (norozo)
- Alternate kanban providers (Linear, ClickUp) — see [ADDITIONAL_FEATURES.md](docs/ADDITIONAL_FEATURES.md)
- Running Ollama in cloud production
