# deepiri-boardman Setup Guide

## Required Credentials

### 1. PLAKY_API_KEY (Required)

1. Log into Plaky at https://app.plaky.com
2. Go to **Settings** → **API** (or your account settings)
3. Generate a new API key
4. Add to `.env`:
   ```
   PLAKY_API_KEY=your_plaky_api_key_here
   ```

### 2. GITHUB_WEBHOOK_SECRET (Optional but recommended)

1. Generate a random secret (e.g., via terminal):
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
2. Add to `.env`:
   ```
   GITHUB_WEBHOOK_SECRET=your_webhook_secret_here
   ```
3. When setting up the webhook in GitHub, use this same secret

### 3. GITHUB_PAT (Optional - only for CLI sync command)

1. Go to GitHub → **Settings** → **Developer settings** → **Personal access tokens** → **Tokens (classic)**
2. Generate new token with scope: `repo` (full control of private repositories)
3. Add to `.env`:
   ```
   GITHUB_PAT=your_github_pat_here
   ```

## GitHub Webhook Setup

For each repo you want to sync:

1. Go to **Repo Settings** → **Webhooks** → **Add webhook**
2. Fill in:
   - **Payload URL**: `https://your-server:8090/api/v1/webhooks/github`
   - **Content type**: `application/json`
   - **Secret**: (same as GITHUB_WEBHOOK_SECRET)
   - **Events**: Select "Issues" and "Pull requests"
3. Click **Add webhook**

## Quick Start

```bash
# 1. Clone/setup
cd /home/joeblack/Documents/Deepiri/deepiri-boardman

# 2. Copy env file and fill in your credentials
cp .env.example .env
nano .env

# 3. Install dependencies (Poetry — see pyproject.toml / poetry.lock)
poetry install

# 4. Run migrations
poetry run alembic upgrade head

# 5. Run locally
poetry run python -m boardman.main

# 6. Verify health
curl http://localhost:8090/api/v1/health
```

## Docker Deployment

The API image installs dependencies with **Poetry** (`Dockerfile`: `poetry install --without dev`). Lockfile: `poetry.lock`.

```bash
docker compose up -d --build
```

## CLI Usage

```bash
poetry run boardman create-task --title "Fix bug" --description "..." --priority high --repo deepiri-platform
poetry run boardman link-pr --pr-url https://github.com/.../pull/123 --task-id XYZ123
poetry run boardman list --status open
poetry run boardman sync --repo owner/repo --dry-run
```