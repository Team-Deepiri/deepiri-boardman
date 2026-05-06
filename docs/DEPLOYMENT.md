# Boardman Deployment Runbook

This runbook covers the first production-like Boardman deployment: Docker Compose on a VPS,
service credentials, GitHub webhooks, Plaky keys, worker setup, and smoke tests.

## Branch and PR Rules

- Work only on `kyle_barnette/feature/<short-description>` branches.
- Do not push directly to `main`, `dev`, or any `*-team-dev` branch.
- Open PRs to `dev` or the required team-dev branch.
- Tag `@Team-Deepiri/support-team` on the PR.
- Include a Plaky task name in the PR body.
- Set Plaky status to `Needs QA` only after code is pushed, the PR exists, and support team is tagged.
- Never set Plaky status to `Done`; that happens only after merge to `main`.

## Services

The Compose stack runs five services:

- `boardman`: FastAPI API and GitHub webhook receiver on port `8090`.
- `boardman-worker`: arq background worker for queued agent/reorder jobs.
- `redis`: private Redis for arq and optional rate limiting.
- `boardman-nginx`: static UI plus `/api` reverse proxy on port `8088`.
- `ollama`: local LLM sidecar on port `11434` inside the Compose network.

Do not confuse `boardman-worker` with the Cloudflare Worker in `worker/`. The Cloudflare Worker
is an optional QA assignment proxy/fallback and is deployed with Wrangler, not Docker Compose.

## Required Secrets

Create a server-local `.env` from `.env.example`. Do not commit `.env`.

| Secret | Purpose | Rotation trigger |
| --- | --- | --- |
| `PLAKY_API_KEY` | Boardman creates, reads, comments on, and updates Plaky tasks. | Staff change, suspected leak, scheduled service key rotation. |
| `GITHUB_PAT` | Boardman reads repos/issues/PRs, discovers org/team data, and initializes/scans repo direction files. | Staff change, permission change, suspected leak, scheduled service key rotation. |
| `GITHUB_WEBHOOK_SECRET` | GitHub webhook HMAC verification. | Suspected leak, webhook rebuild, scheduled service secret rotation. |
| `WORKER_INTERNAL_SECRET` | Bearer token for `/api/v1/assignment/pick-qa`, used by Cloudflare Worker or internal automation. | Suspected leak, worker redeploy, scheduled service secret rotation. |
| `ROUTE_SECRET` | Cloudflare Worker public route bearer token for `/assign-qa`. | Suspected leak, caller change, scheduled service secret rotation. |

Generate strong secrets with:

```bash
openssl rand -hex 32
```

Use dedicated service credentials for production. Do not deploy Kyle's personal PAT or personal
Plaky key except as a temporary emergency bootstrap with an explicit rotation task.

## VPS Bootstrap

On a fresh Ubuntu VPS:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git

curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
```

Log out and back in so the Docker group applies, then clone:

```bash
git clone https://github.com/Team-Deepiri/deepiri-boardman.git
cd deepiri-boardman
git fetch origin --prune
```

If a `dev` or team-dev branch exists, deploy from the approved branch/commit. If only `main`
exists, get explicit approval before treating `main` as the deployment baseline.

## Environment

Create and edit the runtime env:

```bash
cp .env.example .env
nano .env
```

Minimum first-deploy values:

```dotenv
PLAKY_API_KEY=<service-plaky-key>
GITHUB_PAT=<service-github-pat>
GITHUB_WEBHOOK_SECRET=<random-hex-secret>
WORKER_INTERNAL_SECRET=<random-hex-secret>
GITHUB_ORG=deepiri-org
LLM_PROVIDER=ollama
```

For Docker Compose, keep `OLLAMA_BASE_URL` as configured by `docker-compose.yml`
(`http://ollama:11434`) and keep Redis private on the Compose network.

## Start the Stack

Pre-create the SQLite database file before the first Compose start. If this file does not exist,
Docker can create `boardman.db` as a directory during bind mounting, which prevents SQLite from
opening the database.

```bash
test -d boardman.db && rm -rf boardman.db
: > boardman.db
chmod 600 boardman.db
```

```bash
docker compose up -d --build
docker compose ps
```

Pull a small Ollama model first so the agent can respond on modest hardware:

```bash
docker compose exec ollama ollama pull qwen2.5:0.5b
docker compose exec ollama ollama list
```

Check logs:

```bash
docker compose logs --tail=100 boardman
docker compose logs --tail=100 boardman-worker
docker compose logs --tail=100 boardman-nginx
```

## Health Checks

From the VPS:

```bash
curl -fsS http://localhost:8090/api/v1/health
curl -fsS http://localhost:8088/api/v1/health
curl -fsS http://localhost:11434/api/tags
```

Or run the bundled runtime smoke script from the repo root:

```bash
bash scripts/deploy_smoke.sh
```

Expected:

- `boardman` health returns HTTP 200.
- `boardman-nginx` proxies `/api` to `boardman`.
- Ollama lists at least one pulled model.
- Redis is not exposed publicly.
- Logs say the Plaky API key is present and identify the Ollama base URL.
- Webhook `ping` returns HTTP 200 with `pong`.

## GitHub Webhook Setup

For the first smoke test, use one low-risk repo.

GitHub repo settings:

- Payload URL: `https://<boardman-host>/api/v1/webhooks/github`
- Content type: `application/json`
- Secret: value of `GITHUB_WEBHOOK_SECRET`
- Events:
  - Issues
  - Pull requests
  - Pull request reviews
  - Pull request review comments
  - Issue comments

If TLS/domain is not ready yet, use a temporary private HTTP URL only for bootstrap testing and
replace it with HTTPS before wider rollout.

## End-to-End Smoke Test

1. Confirm `docker compose ps` shows all services running.
2. Send GitHub webhook `ping`; delivery should return 200 with `pong`.
3. Create a test GitHub issue in the smoke-test repo.
4. Confirm webhook delivery returns 200.
5. Confirm Boardman logs show the issue event.
6. Confirm Plaky task is created or capture the exact Plaky/API error.
7. Open a test PR linked to the issue with `Closes #<issue-number>`.
8. Confirm Boardman links/comments on the matching Plaky task.
9. Merge or close the test PR only if the test repo is safe.
10. Confirm the configured Plaky status transition runs.

Record the smoke test result in the Plaky task or deployment notes.

## Cloudflare Worker Optional Path

The `worker/` package is a Cloudflare Worker for QA assignment. It is separate from the Compose
`boardman-worker`.

Required Worker secrets/vars:

- `BOARDMAN_URL`: public Boardman URL, for example `https://boardman.example.com`.
- `WORKER_INTERNAL_SECRET`: same value configured in Boardman.
- `ROUTE_SECRET`: bearer token callers use when calling the Worker.
- `QA_TEAM_JSON`: optional fallback data if the Worker is not proxying to Boardman.

Deploy only after the Boardman API is reachable:

```bash
cd worker
npm install
npm run deploy
```

Worker smoke test:

```bash
curl -fsS https://<worker-host>/health
curl -fsS -X POST https://<worker-host>/assign-qa \
  -H "Authorization: Bearer <ROUTE_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{"repo":"Team-Deepiri/deepiri-boardman"}'
```

## Rotation Procedure

Use this order to avoid downtime:

1. Create the replacement key/secret.
2. Update `.env` or platform secret storage.
3. Restart affected services:
   ```bash
   docker compose up -d --force-recreate boardman boardman-worker
   ```
4. Update GitHub webhook secret if rotating `GITHUB_WEBHOOK_SECRET`.
5. Update Cloudflare Worker secrets if rotating worker secrets.
6. Run health checks and one webhook smoke test.
7. Revoke the old key.
8. Update the credential inventory with owner, purpose, date, and next rotation target.

## Rollback

If a deploy breaks:

```bash
git log --oneline -5
git checkout <last-known-good-commit>
docker compose up -d --build
docker compose logs --tail=100 boardman boardman-worker
```

Do not rotate secrets during rollback unless the incident is credential-related.

## First-Deploy Handoff

Capture this before asking for QA:

```text
Branch:
Commit:
Server:
Public URL:
Compose services:
GitHub smoke repo:
Webhook delivery result:
Plaky task result:
Worker path tested: boardman-worker / Cloudflare Worker / both
Known blockers:
Plaky Task:
```
