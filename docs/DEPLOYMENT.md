# Boardman Deployment Runbook

This runbook covers the first production Boardman deployment: Docker Compose on a VPS,
service credentials, GitHub webhooks, Plaky keys, worker setup, and smoke tests.
Production cloud deployments must not run local Ollama/model inference.

## Branch and PR Rules

- Work only on `kyle_barnette/feature/<short-description>` branches.
- Do not push directly to `main`, `dev`, or any `*-team-dev` branch.
- Open PRs to `dev` or the required team-dev branch.
- Tag `@Team-Deepiri/support-team` on the PR.
- Include a Plaky task name in the PR body.
- Set Plaky status to `Needs QA` only after code is pushed, the PR exists, and support team is tagged.
- Never set Plaky status to `Done`; that happens only after merge to `main`.

## Services

The production cloud Compose stack (`docker-compose.prod.yml`) runs three required services:

- `boardman`: FastAPI API and GitHub webhook receiver on port `8090`.
- `boardman-worker`: SQLite background worker for queued agent/reorder jobs.
- `boardman-nginx`: static UI plus `/api` reverse proxy on port `8088`.

Wave one uses `GITHUB_AUTH_MODE=pat`. GitHub App auth is not required unless Joe changes that
deployment decision later.

The local/dev Compose stack (`docker-compose.yml`) also includes `ollama` so CPU/GPU behavior can be
validated locally in the same style as Cyrex. Do not run that Ollama sidecar on the cloud VPS.

`redis` is optional behind the `agent-cache` profile and is only needed when `AGENT_REDIS_URL` is configured.

Do not confuse `boardman-worker` with the Cloudflare Worker in `worker/`. The Cloudflare Worker
is an optional QA assignment proxy/fallback and is deployed with Wrangler, not Docker Compose.

## Queue Path

There is no Kafka-compatible broker in the wave-one Boardman Compose file.

- API async jobs are stored in the SQLite `background_jobs` table inside `boardman.db`.
- `boardman-worker` runs `python -m boardman.sqlite_worker` and claims those SQLite jobs.
- Optional `redis` is cache-only for API/agent data when enabled; it is not the worker queue.
- Kafka or Redpanda would be a future service/adapter decision, not required for first deploy.

## Required Secrets

Create a server-local `.env` from `.env.production.example`. Do not commit `.env`.

| Secret | Purpose | Rotation trigger |
| --- | --- | --- |
| `PLAKY_API_KEY` | Boardman creates, reads, comments on, and updates Plaky tasks. | Staff change, suspected leak, scheduled service key rotation. |
| `GITHUB_PAT` | Boardman reads repos/issues/PRs, discovers org/team data, and initializes/scans repo direction files. | Staff change, permission change, suspected leak, scheduled service key rotation. |
| `GITHUB_WEBHOOK_SECRET` | GitHub webhook HMAC verification. | Suspected leak, webhook rebuild, scheduled service secret rotation. |
| `WORKER_INTERNAL_SECRET` | Bearer token for `/api/v1/assignment/pick-qa` and `sync-field-keys`, used by Cloudflare Worker or internal automation. Also the fallback for `BOARDMAN_API_TOKEN`. | Suspected leak, worker redeploy, scheduled service secret rotation. |
| `BOARDMAN_API_TOKEN` | Bearer token for every privileged `/api/v1` route (see the 🔒 list in the root README): task writes, reconcile, repo classify, plans generate, agent init-direction/scan/jobs/BYOK/session history, mappings, sync-logs. | Suspected leak, operator offboarding, scheduled service secret rotation. |
| `ROUTE_SECRET` | Cloudflare Worker public route bearer token for `/assign-qa`. | Suspected leak, caller change, scheduled service secret rotation. |

Generate strong secrets with:

```bash
openssl rand -hex 32
```

Use dedicated service credentials for production. Do not deploy Kyle's personal PAT or personal
Plaky key except as a temporary emergency bootstrap with an explicit rotation task.

Also rotate any pasted/shared Cloudflare API token if Cloudflare DNS or the optional Cloudflare
Worker path is used. Rotate hosted LLM keys if they were pasted/shared and will be used in
production (`OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`).
GitHub App secrets are not required for wave one because PAT auth is confirmed.

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
cp .env.production.example .env
nano .env
```

Minimum first-deploy values:

```dotenv
PLAKY_API_KEY=<service-plaky-key>
GITHUB_PAT=<service-github-pat>
GITHUB_WEBHOOK_SECRET=<random-hex-secret>
WORKER_INTERNAL_SECRET=<random-hex-secret>
BOARDMAN_API_TOKEN=<random-hex-secret>
ROUTE_SECRET=<random-hex-secret-if-cloudflare-worker-is-used>
BOARDMAN_SECRETS_ROTATED=true
BOARDMAN_TARGET_ENV=vps
GITHUB_AUTH_MODE=pat
BOARDMAN_PUBLIC_URL=https://<boardman-host>
GITHUB_WEBHOOK_EVENTS=issues,pull_request,pull_request_review,pull_request_review_comment,issue_comment
GITHUB_ORG=deepiri-org
LLM_PROVIDER=openai
PR_LINKING_LLM_ENABLED=false
ASSIGNMENT_IDENTITY_LLM_ENABLED=false
```

Do not set `LLM_PROVIDER=ollama` in cloud production. If LLM-dependent behavior is required,
use an approved hosted provider key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `GEMINI_API_KEY`).
If no provider is approved yet, keep LLM-dependent features disabled for the first smoke test.

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
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
```

Check logs:

```bash
docker compose -f docker-compose.prod.yml logs --tail=100 boardman
docker compose -f docker-compose.prod.yml logs --tail=100 boardman-worker
docker compose -f docker-compose.prod.yml logs --tail=100 boardman-nginx
```

## Health Checks

From the VPS:

```bash
curl -fsS http://localhost:8090/api/v1/health
curl -fsS http://localhost:8088/api/v1/health
```

Or run the bundled runtime smoke script from the repo root:

```bash
BOARDMAN_COMPOSE_FILE=docker-compose.prod.yml bash scripts/deploy_smoke.sh
```

Expected:

- `boardman` health returns HTTP 200.
- `boardman-nginx` proxies `/api` to `boardman`.
- Ollama smoke checks are skipped because production cloud does not run local LLM inference.
- Redis remains disabled unless `--profile agent-cache` is explicitly enabled; if enabled, keep it private.
- Logs say the Plaky API key is present.
- Webhook `ping` returns HTTP 200 with `pong`.

## Privileged API Routes (bearer token)

`docker-compose.prod.yml` binds the API to `127.0.0.1:8090` and the nginx vhost is the
public entry point. That protects the *port*, not the *routes*: nginx proxies all of
`/api/` through, so any unauthenticated write route is reachable from the internet.

Every route that writes to Plaky or GitHub with the server's own credentials, rewrites a
config file on disk, or spends LLM budget therefore requires:

```
Authorization: Bearer $BOARDMAN_API_TOKEN
```

- Falls back to `WORKER_INTERNAL_SECRET`, so a deployment that predates this var stays
  protected rather than silently opening up.
- If **neither** is set, these routes return **404** — a misconfigured box does not
  advertise them. Verify before declaring go-live.
- Compared in constant time. Case-insensitive scheme; the token itself must match exactly.

Check the deployment is actually closed:

```bash
# must be 401 (or 404 if no secret is configured) -- never 200
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  http://localhost:8090/api/v1/reconcile/Team-Deepiri/deepiri-boardman

# must be 200
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  -H "Authorization: Bearer $BOARDMAN_API_TOKEN" \
  http://localhost:8090/api/v1/reconcile/Team-Deepiri/deepiri-boardman
```

The `boardman-ui` bundle is public and cannot embed a secret, so `POST /api/v1/tasks` and
`POST /api/v1/repos/classify` need the token too. Operators paste it into the UI sidebar
("API token"); it is held in `sessionStorage` and cleared when the tab closes. Agent chat
stays open, but an anonymous caller's `allow_writes: true` is downgraded to read-only, so the
Plaky mutation tools are only reachable with the token.

`scripts/production_checklist.py` reads `BOARDMAN_API_TOKEN` (falling back to
`WORKER_INTERNAL_SECRET`) and authenticates its reconcile and agent-write checks
automatically.

**Known limitation:** this is a shared bearer token, not user authentication. Anyone
holding it has every privileged capability. It is appropriate for an internal tool behind a
trusted network; for per-user identity, put a session/auth layer in front at nginx and drop
the token from the browser entirely.

## Public nginx vhost (boardman.deepiri.com)

`deploy/nginx/boardman.deepiri.com.conf` is the box's public entry point and serves **no UI**,
so it does not proxy the whole API tree. Its catch-all `location /api/` returns **403** and only
three paths are proxied:

| Path | Why it is public |
| --- | --- |
| `POST /api/v1/webhooks/github` | GitHub webhook delivery (HMAC-verified) |
| `POST /api/v1/assignment/pick-qa` | Cloudflare Worker (bearer `WORKER_INTERNAL_SECRET`) |
| `GET /health` | monitoring |

Everything else — the org/repo listings, Plaky board and user rosters, support-team roster, LLM
model list, and agent chat (which spends LLM budget on the server's key) — is **not reachable
through this vhost**, even though some of those routes are unauthenticated inside the app. Call
them over the docker network or on `127.0.0.1:8090` instead.

Two consequences worth knowing:

- **Adding a route to the app does not publish it.** To expose a new endpoint deliberately, add
  an explicit `location = /api/v1/...` block with its own `proxy_pass`.
- `deploy/nginx/default.conf` (the `boardman-ui` vhost on `:8088`) intentionally still proxies
  all of `/api/`, because the SPA needs those reads. Do not copy the 403 catch-all there — it
  would break the UI.

Verify after any nginx change. The checked-in validator runs the real `nginx -t` over **both**
vhosts, and needs neither Docker nor root:

```bash
bash scripts/validate_nginx_conf.sh
```

It wraps each fragment in a throwaway prefix, substituting only what is specific to the VPS — the
Let's Encrypt cert paths, the privileged `listen 80`/`443` ports, and the docker-internal
`boardman` upstream — and hands the result to a real nginx, so `location` blocks, `proxy_pass`
targets, `ssl_*` settings and `http2 on` are all parsed exactly as written. If no nginx is on
`PATH` it builds one into a temp dir (needs `gcc`, `make`, OpenSSL headers, network); point
`NGINX_BIN` at an existing binary to skip that.

Add `--live` to also check that the vhost *routes* as it reads, not just that it parses:

```bash
bash scripts/validate_nginx_conf.sh --live deploy/nginx/boardman.deepiri.com.conf
```

`nginx -t` alone cannot see the failure this vhost exists to prevent. If
`location = /api/v1/webhooks/github` ever lost precedence to the `location /api/` catch-all — a
reordered block, someone tidying the `=` away — every GitHub delivery would 403 and `nginx -t`
would still pass. `--live` boots the real nginx in front of a stub backend and asserts per path
whether the request reached the backend: the three allowlisted paths must be proxied, and
`/api/v1/tasks`, `/api/v1/repos/org`, `/api/v1/llm/models`, `POST /api/v1/agent/chat` and
`POST /api/v1/repos/classify` must be stopped at the edge. Both regressions were confirmed to
pass `nginx -t` and fail this check.

CI runs both automatically: `nginx -t` against the production `nginx:alpine` image it already
builds, and the live check. `pytest` runs them too and skips when no nginx is available.

To check against the exact production image by hand:

```bash
docker run --rm -v "$PWD/deploy/nginx:/etc/nginx/conf.d" -v "$PWD/deploy/nginx/boardman.deepiri.com.conf:/etc/nginx/conf.d/default.conf:ro" nginx:alpine nginx -t
```

### What these checks cannot cover

The VPS's own nginx main config (the one that `include`s this fragment) is not in this repository,
so two things stay unverified until you check them on the host:

- **The include wiring itself.** That the fragment is actually included in the `http {}` block of
  the running nginx, and that no other vhost on the box also claims `boardman.deepiri.com`. A
  `server_name` collision would make the more specific vhost win or lose depending on load order.
- **That the upstream resolves on the box.** The live check substitutes `boardman` with loopback.
  On the VPS, `boardman:8090` must resolve from the platform's nginx container, which needs the
  shared/external network attach described at the top of the fragment.

After wiring it up, confirm from outside the host that the boundary holds:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://boardman.deepiri.com/api/v1/tasks        # 403
curl -s -o /dev/null -w '%{http_code}\n' https://boardman.deepiri.com/api/v1/repos/org    # 403
curl -s -o /dev/null -w '%{http_code}\n' https://boardman.deepiri.com/health             # 200
```

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

1. Confirm `docker compose -f docker-compose.prod.yml ps` shows all services running.
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

## Agent Redis Cache & Production Performance Runbook

Boardman includes optional Redis caching for agent session states, Plaky board schemas, and
repository contexts. Enabling Redis significantly reduces tool-calling latency by avoiding
repetitive 5–7s Plaky API board schema fetches and caching multi-turn agent context.

### 1. Enabling the Agent Redis Cache

The `docker-compose.prod.yml` file already declares an isolated, resource-capped Redis service
(`deepiri-boardman-redis-cache`, 256MB cap, alpine) under the `agent-cache` profile on the
internal `deepiri-network`.

To enable on the production VPS:

1. Start the Redis cache service:
   ```bash
   docker compose -f docker-compose.prod.yml --profile agent-cache up -d redis
   ```
2. Configure `.env` on the host:
   ```dotenv
   AGENT_REDIS_URL=redis://redis:6379/1
   ```
3. Restart the `boardman` API container to pick up the cache:
   ```bash
   docker compose -f docker-compose.prod.yml up -d --no-deps boardman
   ```
4. Verify from API logs:
   ```bash
   docker compose -f docker-compose.prod.yml logs --tail=50 boardman | grep -i redis
   ```
   Look for: `Redis agent cache connected on db 1` or cache initialization logs.

> [!NOTE]
> **Worker Isolation**: As documented in `boardman.cache.agent_redis`, `boardman-worker` must
> keep `AGENT_REDIS_URL=""`. The SQLite worker handles queued jobs independently and must never
> take a runtime dependency on Redis. Only the API container connects to Redis.

### 2. Performance Tuning Under Constraints

If the production agent feels slow or unreliable, review these operational levers:

- **Model Selection (`LLM_MODEL`)**: When `LLM_PROVIDER=openrouter` is set without an explicit
  `LLM_MODEL`, Boardman defaults to the free-tier `minimax/minimax-m3:free`. Free-tier models
  suffer from high queue wait times and strict rate limits (429 errors causing backoff delays).
  For production responsiveness, specify a fast, cost-effective hosted model in `.env`, such as:
  ```dotenv
  LLM_MODEL=google/gemini-2.0-flash-001
  # or: LLM_MODEL=anthropic/claude-3-5-haiku
  # or: LLM_MODEL=openai/gpt-4o-mini
  ```
- **History Trimming (`AGENT_MAX_HISTORY`)**: Default is 16. In multi-turn chat sessions with tool
  outputs, large histories balloon token payloads. Tuning `AGENT_MAX_HISTORY=8` in `.env` reduces
  time-to-first-token noticeably on chat turns.
- **Plaky Schema Cache TTL (`PLAKY_BOARD_SCHEMA_CACHE_TTL_SECONDS`)**: Plaky API schema fetches
  take ~5–7s. Raising the cache TTL to `300.0` (5 minutes) prevents repetitive schema roundtrips
  during agent conversations.
- **Pre-warmed Repo Knowledge (`REPO_KNOWLEDGE_SWEEP_ENABLED`)**: Keep this `true` so the
  `boardman-worker` pre-warms repository snapshots in SQLite asynchronously, preventing synchronous
  GitHub tree fetches during user queries.
- **Tool Recursion Bounds (`AGENT_RECURSION_LIMIT`)**: Default `0` automatically enforces 10 (read)
  / 16 (write) steps. Pinning to `AGENT_RECURSION_LIMIT=8` prevents runaway tool calls on complex
  prompts.

## Cloudflare Worker Optional Path

The `worker/` package is a Cloudflare Worker for QA assignment only. It is separate from the Compose
`boardman-worker` and is not the main Boardman backend deployment.

Required Worker secrets/vars:

- `BOARDMAN_URL`: public Boardman URL, for example `https://boardman.example.com`.
- `WORKER_INTERNAL_SECRET`: same value configured in Boardman.
- `ROUTE_SECRET`: bearer token callers use when calling the Worker.
- `QA_TEAM_JSON`: optional fallback data if the Worker is not proxying to Boardman.

The Worker should only expose `/health` and `/assign-qa`.

Deploy only after the Boardman API is reachable:

```bash
cd worker
npm ci
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
