# deepiri-boardman Setup Guide

> For full quick start, CLI list, and Docker prod path see [README.md](README.md). Agents: see [AGENTS.md](AGENTS.md).

## Required Credentials

### 1. PLAKY_API_KEY (Required)

1. Log into Plaky at https://app.plaky.com
2. Go to **Settings** → **API** (or your account settings)
3. Generate a new API key
4. Add to `.env`:
   ```
   PLAKY_API_KEY=your_plaky_api_key_here
   ```

### 2. GITHUB_WEBHOOK_SECRET (Required for production)

1. Generate a random secret:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
2. Add to `.env`:
   ```
   GITHUB_WEBHOOK_SECRET=your_webhook_secret_here
   ```
3. Use the same secret when registering GitHub webhooks

### 3. GITHUB_PAT (Required for scan, init, agent tools)

1. GitHub → **Settings** → **Developer settings** → **Personal access tokens**
2. Generate token with `repo` scope (and `read:org` if using QA team roster)
3. Add to `.env`:
   ```
   GITHUB_PAT=your_github_pat_here
   ```

Also required for `boardman init`, `boardman scan`, and agent GitHub tools. Optional for webhook-only deploys.

## GitHub Webhook Setup

For each repo you want to sync:

1. **Repo Settings** → **Webhooks** → **Add webhook**
2. Fill in:
   - **Payload URL**: `https://your-server:8090/api/v1/webhooks/github`
   - **Content type**: `application/json`
   - **Secret**: (same as `GITHUB_WEBHOOK_SECRET`)
   - **Events**: Issues, Pull requests, Pull request reviews, Issue comments
3. **Add webhook**

## Quick Start

```bash
git clone <repo-url> deepiri-boardman
cd deepiri-boardman

cp .env.example .env
# Edit .env with PLAKY_API_KEY, GITHUB_PAT, GITHUB_WEBHOOK_SECRET

poetry install --with dev
poetry run alembic upgrade head
poetry run python -m boardman.main

curl http://localhost:8090/api/v1/health
```

## Docker Deployment

Poetry lockfile is used in the API image (`Dockerfile`: `poetry install --without dev`).

**Local/dev** (includes optional Ollama):

```bash
./scripts/deploy_preflight.sh
docker compose up -d --build
```

**Production** (no Ollama):

```bash
cp .env.production.example .env
BOARDMAN_COMPOSE_FILE=docker-compose.prod.yml bash scripts/deploy_preflight.sh
docker compose -f docker-compose.prod.yml up -d --build
BOARDMAN_COMPOSE_FILE=docker-compose.prod.yml bash scripts/deploy_smoke.sh
```

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for VPS credentials, worker, and rotation.

## CLI Usage (common)

```bash
poetry run boardman create-task --title "Fix bug" --description "..." --priority Medium --github-repo owner/repo
poetry run boardman link-pr --pr-url https://github.com/.../pull/123 --task-id ID --board-id ID
poetry run boardman list --status "In Progress" --board-id ID
poetry run boardman sync --repo owner/repo --dry-run
poetry run boardman scan owner/repo --dry-run
poetry run boardman agent chat -m "Summarize open tasks"
poetry run boardman readiness
poetry run boardman doctor
```

Full command list: [README.md](README.md).
