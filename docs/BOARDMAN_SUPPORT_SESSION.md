# Boardman Support Session Checklist

Use this checklist during the Wednesday support session to keep decisions and deployment steps tight.

## 1) Confirm Production Scope

- Cloud production stack: `boardman`, `boardman-worker`, `boardman-nginx`.
- Do **not** run Ollama in cloud production.
- Cloudflare Worker is optional and only for `/assign-qa` and `/health`.

## 2) Required Secrets (Rotated Values Only)

- `PLAKY_API_KEY`
- `GITHUB_PAT` (or confirm GitHub App migration plan)
- `GITHUB_WEBHOOK_SECRET`
- `WORKER_INTERNAL_SECRET`
- `ROUTE_SECRET` (only if Cloudflare Worker path is enabled)

## 3) Production Compose Path

```bash
cp .env.production.example .env
test -d boardman.db && rm -rf boardman.db
: > boardman.db
chmod 600 boardman.db
BOARDMAN_COMPOSE_FILE=docker-compose.prod.yml bash scripts/deploy_preflight.sh
docker compose -f docker-compose.prod.yml up -d --build
BOARDMAN_COMPOSE_FILE=docker-compose.prod.yml bash scripts/deploy_smoke.sh
```

## 4) Pass/Fail Signals

### Pass

- `deploy_preflight.sh` reports `0 failure(s)` for `BOARDMAN_COMPOSE_FILE=docker-compose.prod.yml`.
- `deploy_smoke.sh` reports `0 failure(s)` for `BOARDMAN_COMPOSE_FILE=docker-compose.prod.yml`.
- `GET /api/v1/health` returns HTTP 200 through both `:8090` and `:8088/api`.
- GitHub webhook `ping` returns 200 + `pong`.

### Fail

- Production env uses `LLM_PROVIDER=ollama`.
- Any required service in production compose is not running.
- Webhook delivery returns non-200.
- Plaky task creation/linking fails in smoke test.

## 5) Local Validation Path (Optional, Non-Production)

Use local/dev stack only to validate CPU/GPU Ollama behavior in the Cyrex style:

```bash
bash scripts/deploy_preflight.sh
docker compose up -d --build
bash scripts/deploy_smoke.sh
```

This local path is separate from cloud production.
