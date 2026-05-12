# deepiri-boardman

GitHub ↔ Plaky sync automation service.

## Overview

Automatically syncs GitHub issues and pull requests to Plaky tasks:

- **Issue opened** → Creates Plaky task tagged with repo name
- **PR opened** → Adds PR link as comment on linked Plaky task
- **PR merged** → Updates Plaky task status (configurable, default: `in_review`)

## Features

- FastAPI REST API on port 8090
- GitHub webhook receiver with HMAC verification
- SQLite database for issue↔task mapping
- CLI for manual operations (`boardman`)
- **`repos.yml`** routing → Plaky table hints on new tasks (webhook + scan)
- **AI scan** (`boardman scan`, `POST /api/v1/agent/scan`) — `DIRECTION.md` + GitHub + LLM → Plaky tasks
- **Agent chat** — LangChain tool-calling agent (Plaky + GitHub + local repo tools) with **`allow_writes`** guardrail; falls back to plain chat if tools fail
- **`boardman-ui`** — Vite/React chat + floating messages panel (Cyrex-style); dev proxy or nginx in Docker
- **Docker Compose** — `boardman` API, **nginx** (static UI + `/api` → API), **Ollama** sidecar
- Docker deployment ready

## Quick Start

Python dependencies are managed with **[Poetry](https://python-poetry.org/)** (`pyproject.toml` + `poetry.lock`).

```bash
# Install Poetry: https://python-poetry.org/docs/#installation
cd deepiri-boardman
poetry install --with dev

# Configure (see SETUP.md)
cp .env.example .env
# Edit .env with your PLAKY_API_KEY, optional GITHUB_PAT, LLM_*

poetry run alembic upgrade head

# Run API
poetry run python -m boardman.main
# or: poetry shell && python -m boardman.main

# Test health endpoint
curl http://localhost:8090/api/v1/health
```

## CLI Commands

Use `poetry run boardman …` (or activate `poetry shell` first).

```bash
poetry run boardman create-task --title "Task" --description "..." --priority "Medium" --github-repo REPO --status "In Progress" --type "Feature" --board-id id --group-id id --engineer-id id --auto-assign-team
poetry run boardman create-task --title "Task" --github-repo "REPO REPO"  # also supports comma-separated
poetry run boardman update-task --task-id id --status "Needs QA" --priority "High" --type "Feature" --auto-assign-qa --github-repo "REPO" --board-id ID
poetry run boardman create-subtask --parent-task-id ID --title "Task" --description "..." --priority "High" --status "In Progress" --type "Feature" --github-repo "REPO1 REPO2" --board-id ID --group-id ID --no-auto-assign-qa
poetry run boardman link-pr --pr-url "https://github.com/.../pull/123" --task-id ID --board-id ID
poetry run boardman list --status "In QA" --board-id ID --format table
poetry run boardman sync --repo repo --board-id ID --group-id ID --dry-run #dry-run is optional, requieres that specified repo has issues
poetry run boardman register repo --category ai --table "AI Bugs / What to DO"
poetry run boardman scan ORG/REPO --dry-run
poetry run boardman doctor
poetry run boardman agent chat -m "What should we prioritize?"
poetry run boardman agent ask -m "List open Plaky tasks"
poetry run boardman init REPO
poetry run boardman status --repo REPO
poetry run boardman scan-all --dry-run
```

### UI (local)

```bash
cd boardman-ui && npm install && npm run dev
# API on :8090, UI on :5176 (proxies /api → boardman)
```

### Full stack (Docker)

```bash
./scripts/deploy_preflight.sh
docker compose up -d --build
# API http://localhost:8090
# UI + proxy http://localhost:8088  (nginx → boardman)
# Ollama http://localhost:11434  (set OLLAMA_BASE_URL=http://ollama:11434 in .env for compose)
```

For NVIDIA GPU hosts, configure Docker's default runtime once:

```bash
sudo nvidia-ctk runtime configure --runtime=docker --set-as-default
sudo systemctl restart docker
```

Boardman still uses the same `docker compose up -d --build` command. CPU-only hosts leave
`OLLAMA_DOCKER_RUNTIME` unset; NVIDIA hosts use the default runtime path above, and Ollama sees
`NVIDIA_VISIBLE_DEVICES=all`. If you cannot set Docker's default runtime, set
`OLLAMA_DOCKER_RUNTIME=nvidia` in `.env` for this service only.

Deployment smoke checks (after the stack is up):

```bash
bash scripts/deploy_smoke.sh
```

## API Endpoints

- `GET /api/v1/health` - Health check
- `POST /api/v1/webhooks/github` - GitHub webhook receiver
- `POST /api/v1/tasks` - Create Plaky task
- `GET /api/v1/tasks` - List Plaky tasks
- `GET /api/v1/mappings` - List issue↔task mappings
- `POST /api/v1/tasks/{id}/link-pr` - Link PR to task
- `POST /api/v1/agent/chat` - Agent chat (`message`, `session_id?`, `repo?`, `provider?`, `model?`, **`allow_writes`**)
- `GET /api/v1/agent/sessions/{id}/history` - Session transcript
- `DELETE /api/v1/agent/sessions/{id}` - Drop session
- `POST /api/v1/agent/scan` - `{ "repo": "owner/name", "dry_run": false, ... }`
- `POST /api/v1/agent/init-direction` - opens a PR for `DIRECTION.md` using signed-in `gh` user (`{ "repo": "owner/name", "branch?": "main", "force?": false }`)

## Configuration

See `.env.example` for all options. Key variables:

- `PLAKY_API_KEY` - Required. Your Plaky API key
- `GITHUB_WEBHOOK_SECRET` - Optional. For HMAC verification
- `GITHUB_PAT` - Optional. For CLI sync command
- `gh` CLI auth - Required for `boardman init` and `/api/v1/agent/init-direction` (must be signed in with repo write access)
- `PLAKY_PR_MERGE_STATUS` - Status to set on PR merge (default: `in_review`)
- `LLM_PROVIDER`, `LLM_MODEL`, `OLLAMA_BASE_URL`, cloud API keys — see `.env.example`
  - OpenRouter is supported via `LLM_PROVIDER=openrouter`, `OPENROUTER_API_KEY`, and provider-prefixed model IDs like `anthropic/claude-3.5-sonnet`.

Deployment runbook: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

Full roadmap: [docs/PLAN.md](docs/PLAN.md).

## Tests

```bash
poetry install --with dev
poetry run pytest tests/
```
