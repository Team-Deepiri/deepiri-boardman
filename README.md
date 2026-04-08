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
- Docker deployment ready

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Configure (see SETUP.md)
cp .env.example .env
# Edit .env with your PLAKY_API_KEY

# Run
python -m boardman.main

# Test health endpoint
curl http://localhost:8090/api/v1/health
```

## CLI Commands

```bash
boardman create-task --title "Task" --description "..." --priority medium --repo my-repo
boardman link-pr --pr-url https://github.com/.../pull/123 --task-id XYZ
boardman list --status open
boardman sync --repo owner/repo
```

## API Endpoints

- `GET /api/v1/health` - Health check
- `POST /api/v1/webhooks/github` - GitHub webhook receiver
- `POST /api/v1/tasks` - Create Plaky task
- `GET /api/v1/tasks` - List Plaky tasks
- `GET /api/v1/mappings` - List issue↔task mappings
- `POST /api/v1/tasks/{id}/link-pr` - Link PR to task

## Configuration

See `.env.example` for all options. Key variables:

- `PLAKY_API_KEY` - Required. Your Plaky API key
- `GITHUB_WEBHOOK_SECRET` - Optional. For HMAC verification
- `GITHUB_PAT` - Optional. For CLI sync command
- `PLAKY_PR_MERGE_STATUS` - Status to set on PR merge (default: `in_review`)