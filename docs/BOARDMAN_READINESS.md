# Boardman Readiness Status

Boardman is deployed from this standalone repo. Platform-level services can integrate with it later,
but this repo owns the first production path for the Boardman API, worker, UI/proxy, webhooks, and
optional Cloudflare Worker helper.

## Status Command

Run an offline status check before asking for go-live:

```bash
poetry run boardman readiness
```

For a template-only review that does not require a real `.env`:

```bash
poetry run boardman readiness --env-file .env.production.example || true
```

The `|| true` is only for local preview. The template intentionally reports placeholder secrets as
failures.

For machine-readable output:

```bash
poetry run boardman readiness --format json
```

Use strict mode in CI or a release gate when pending decisions should fail the job:

```bash
poetry run boardman readiness --strict-pending
```

The command never prints secret values. It reports only whether required values are present,
placeholder-like, rotated, or still pending.

## Plaky Inventory

When someone has the Plaky key/access, they can export the IDs needed for `repos.yml` and
`team_assignments.yml`:

```bash
poetry run boardman plaky-inventory
poetry run boardman plaky-inventory --board-id <board-id>
poetry run boardman plaky-inventory --board-id <board-id> --format json
```

This prints board IDs, group IDs, field keys, status option IDs/values, and user IDs without
printing the Plaky API key.

## Acceptance Fixtures

For engineering-completion checks without production access, use the acceptance fixture files:

- `.env.acceptance.example`
- `repos.acceptance.yml`
- `team_assignments.acceptance.yml`

Offline acceptance SQLite prep:

```bash
bash scripts/acceptance_prepare.sh
```

These fixtures intentionally enforce wave-one decisions:

- `GITHUB_AUTH_MODE=pat`
- `BOARDMAN_SECRETS_ROTATED=true`
- Hosted-LLM mode only (`LLM_PROVIDER=openai`, no local Ollama production behavior)

These files are intentionally fake and must never contain real secrets.

## Current Owner-Area Reply

Use this in the Boardman chat when someone asks for concrete status:

```text
Owner area: Boardman standalone repo readiness/deployment.

Done fully:
- Standalone repo confirmed: Team-Deepiri/deepiri-boardman.
- Production shape exists: FastAPI API, boardman-worker, nginx/UI proxy, docker-compose.prod.yml.
- GitHub auth mode confirmed for wave one: PAT.
- Preflight and smoke scripts exist for Docker deployment.
- Readiness command added: poetry run boardman readiness.
- Production env template now tracks target env, GitHub auth mode, public URL, webhook events, and credential rotation gate.

Still in progress:
- Fill real target .env on the server.
- Fill repo -> Plaky board/group IDs in repos.yml.
- Confirm Plaky field IDs and member IDs in team_assignments.yml.
- Run deploy_preflight, start compose, then run deploy_smoke on the target host.

Blocked by what/who:
- Joe/Kyle decision: target environment/public URL.
- Plaky admin/access person: board/group IDs, field IDs, and status option values.
- Security/deployment person: rotate every pasted/shared credential before deploy.

What I need next:
- Approve the wave-one target host and hostname.
- Provide/confirm Plaky board/group/field IDs.
- After secrets are rotated and stored on the host, I can run readiness, preflight, compose up, smoke, and write the final go-live pass/fail note.
```

## Wave-One Boundary

Boardman production for wave one is:

- `boardman`: FastAPI API and GitHub webhook receiver.
- `boardman-worker`: SQLite background worker.
- `boardman-nginx`: UI/proxy entrypoint.
- `boardman.db`: persistent SQLite file.
- `repos.yml` and `team_assignments.yml`: deployment config.
- `worker/`: optional Cloudflare Worker for `/assign-qa`, not the main backend.

## Queue Path

The wave-one Boardman Docker Compose path does not run Kafka, Redpanda, or any Kafka-compatible
broker.

Current queue path:

- API enqueues async jobs into SQLite table `background_jobs` in `boardman.db`.
- `boardman-worker` runs `python -m boardman.sqlite_worker`.
- The worker claims jobs from SQLite and runs registered handlers.
- Optional `redis` is only for API/agent cache when the `agent-cache` profile is enabled.

If Joe wants a Kafka-compatible path later, that is a separate queue adapter/service decision.
It is not required for the standalone Boardman wave-one deploy.

Boardman production is not the full Deepiri platform. Norozo, API gateway, auth service,
engagement service, task orchestrator, Kubernetes, managed databases, vector DBs, GPU hosts,
and local model inference should stay outside the wave-one Boardman deploy unless Joe explicitly
changes the scope.

## Go-Live Gates

- Target environment and public hostname are confirmed.
- GitHub auth mode is PAT for wave one.
- GitHub webhook payload URL is `https://<boardman-host>/api/v1/webhooks/github`.
- Webhook events are enabled: issues, pull requests, pull request reviews, pull request review comments, issue comments.
- All pasted/shared credentials are rotated and stored only in the target environment.
- `repos.yml` has repo names and Plaky board/group IDs.
- `team_assignments.yml` has real Plaky field IDs and member IDs or confirmed auto-matching.
- `poetry run boardman readiness --strict-pending` passes on the target host.
- `BOARDMAN_COMPOSE_FILE=docker-compose.prod.yml bash scripts/deploy_preflight.sh` passes.
- `docker compose -f docker-compose.prod.yml up -d --build` starts API, worker, and nginx.
- `BOARDMAN_COMPOSE_FILE=docker-compose.prod.yml bash scripts/deploy_smoke.sh` passes.
- A test webhook creates/updates a Plaky item without duplicates on replay.
- PR link/review/comment status tests pass.
- Final pass/fail note is posted with host, commit, smoke repo, webhook result, Plaky result, and rollback commit.

## Secrets To Rotate

Rotate these before deployment if they were pasted/shared or used from a personal account:

- `PLAKY_API_KEY`
- `GITHUB_PAT`
- `GITHUB_WEBHOOK_SECRET`
- `WORKER_INTERNAL_SECRET`
- `ROUTE_SECRET`
- Cloudflare API token, if Cloudflare DNS or the optional Cloudflare Worker path is used.
- Hosted LLM API keys, if used in production: OpenAI, OpenRouter, Anthropic, Gemini.
- Database or Redis passwords, if a managed DB/cache is introduced later.

GitHub App secrets are not part of wave one because Joe confirmed PAT.
