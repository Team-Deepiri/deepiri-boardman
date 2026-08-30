# Boardman on the cloud-portal VPS — fit analysis + deployment plan

Companion to [deepiri-platform PR #304](https://github.com/Team-Deepiri/deepiri-platform/pull/304)
and its docs (`CHEAP_ONE_BOX_VPS.md`, `NETCUP_VPS_1000_G12_DEPLOYMENT_PLAN.md`). This asks the
next question: can **deepiri-boardman** share that same box, and what does hosting it there
actually take.

**Short answer: yes, easily, on resources — the open questions are about ports, secrets, and
routing, not whether it fits.**

---

## The box (from PR #304)

**Netcup VPS 1000 G12** — 4 vCore / 8 GB DDR5 ECC / 256 GB NVMe, ~€11.56/mo (hourly SKU).

Measured with the 11-service `deepiri-platform` cloud-portal stack already running on it:

| State | Memory | CPU |
|---|---|---|
| Idle | ~316 MiB | ~2.0% |
| Under gateway-routed load (the path real traffic takes) | ~440 MiB | ~1.66 core-eq |
| Synthetic worst case (4 backend services saturated directly) | ~590 MiB | ~4.46 core-eq |

Even the synthetic worst case is ~7% of 8 GB. **Memory was never the constraint on this box —
CPU is**, and only under a load pattern (multiple services saturated simultaneously,
bypassing the gateway) that real traffic doesn't produce.

**Hard rule already established for this box** (per `CHEAP_ONE_BOX_VPS.md`): no Cyrex, LIS,
speech, Ollama, MLflow, Milvus, Kafka, or messaging here — those stay on
`deepiri-control-plane`. This matters for Boardman too, see below.

---

## Boardman's own footprint

Measured directly (this session, `uvicorn boardman.main:app`, single worker, idle, before any
request): **~107 MB RSS** for the API process alone. The background worker
(`boardman.sqlite_worker` / a Postgres-backed equivalent — see the Postgres migration PR) does
comparable heavy imports (LangChain, the tool registry) without the FastAPI/uvicorn layer, so
budget the same order of magnitude, not less.

Estimated full-stack footprint if Boardman runs here:

| Component | Estimated idle RSS | Notes |
|---|---|---|
| `boardman` (API) | ~110 MB | Measured directly this session |
| `boardman-worker` | ~80-110 MB | Same import weight, no HTTP layer |
| `boardman-nginx` (UI + proxy) | ~5-10 MB | Static file server + reverse proxy |
| Postgres (own instance, small dataset) | ~30-50 MB | See "shared vs. separate Postgres" below |
| **Total** | **~250-300 MB** | Same order of magnitude as the entire 11-service platform stack |

Against a box that had **~7.5 GB free** even under the platform's own load testing, adding
Boardman is not a capacity question. **Boardman never needs Ollama in this deployment** —
`docker-compose.prod.yml` already omits it and expects a hosted LLM provider
(`LLM_PROVIDER=openai` or similar), which is exactly consistent with this box's own
no-heavy-AI rule.

CPU: Boardman's workload is webhook-driven and I/O-bound (calls out to GitHub, Plaky, and a
hosted LLM API) rather than CPU-bound — it does not compete with the platform stack's CPU
ceiling the way a local model or a CPU-heavy service would. Not measured under simultaneous
load with the platform stack; worth a real `docker stats` pass once both are actually running
together, same caveat the platform docs give themselves.

---

## What has to be decided before deploying (not resource questions — architecture ones)

### 1. Domain / routing

The platform's `ops/nginx/cloud-prod.conf` is a single-domain, catch-all (`server_name _`)
config with one Let's Encrypt cert. Boardman ships its **own** nginx+UI container
(`boardman-nginx`, port 8088) and API (port 8090) in `docker-compose.prod.yml`. Two ways to
combine them on one box, pick one:

- **Subdomain (recommended)** — e.g. `boardman.<domain>`. Add a second `server{}` block
  (or a second nginx instance + a second Let's Encrypt cert via `certbot certonly -d
  boardman.<domain>`) that proxies to Boardman's containers over the shared Docker network.
  Cleanest separation; GitHub's webhook URL (`https://boardman.<domain>/api/v1/webhooks/github`)
  reads unambiguously as Boardman's, not the platform's.
- **Path-based** (`<domain>/boardman/...`) — works, but Boardman's own UI build and API
  routes assume root-relative paths; would need `VITE_API_BASE`/router-base changes on the
  frontend and is more fragile for the webhook path specifically (GitHub retries a fixed URL
  forever if it 404s after a routing change).

Either way: **bind Boardman's containers to `127.0.0.1` only** (not `0.0.0.0`), same pattern
the platform stack uses — the shared nginx is the only public-facing edge on this box.

### 2. Shared vs. separate Postgres

The Postgres migration (this repo, merged) makes `DATABASE_URL` a `postgresql+asyncpg://` URL
with no other requirement. Two options:

- **Separate Postgres container for Boardman** — matches the resource budget above (~30-50
  MB), keeps failure domains and backup/restore independent from the platform's
  `postgres-platform`, and avoids any cross-service credential/permission entanglement. This
  is the safer default and what I'd recommend given the trivial resource cost.
- **A second database inside the platform's existing `postgres-platform` instance** — saves
  one container's worth of idle memory (~30-50 MB, not meaningful at this box's headroom) but
  means Boardman's schema changes, connection pool, and any Postgres-level lockup or backup
  event now share fate with the platform's own database. **Not recommended** unless there's a
  specific ops reason (e.g. wanting exactly one thing to back up) to prefer it.

### 3. Secrets on the box

Boardman needs its own env, separate from the platform's `ops/k8s/secrets/.env`:

- `PLAKY_API_KEY`, `GITHUB_PAT`, `GITHUB_WEBHOOK_SECRET`
- `DATABASE_URL` (Postgres DSN — see above), `POSTGRES_PASSWORD` if running its own instance
- `LLM_PROVIDER` + the matching API key (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / etc.) —
  **no Ollama key needed, none should be configured on this box**
- `CORS_ORIGINS` set to whatever domain/subdomain is chosen above
- `ROUTE_SECRET` (worker↔API internal auth, if applicable — check current `.env.example`)

None of these should live in the platform's `cloud-portal-secrets.7z` bundle — Boardman's
secrets are a separate file (`ops/k8s/secrets/.env` under `/opt/deepiri/deepiri-boardman`, not
shared with `/opt/deepiri/deepiri-platform`), same isolation principle the platform docs
already apply to control-plane vs. cloud-portal secrets.

### 4. GitHub webhook reachability

Once a domain/subdomain and TLS are live, register the webhook URL
(`https://boardman.<domain>/api/v1/webhooks/github`) in the GitHub org/repo webhook settings
with the `GITHUB_WEBHOOK_SECRET` above. Until DNS + TLS are actually pointed at this box,
Boardman can still run in `TESTING_LIVE_PLAKY` polling mode (already supported, see README) as
a bridge — no public endpoint required, at the cost of near-real-time sync becoming
poll-interval-delayed instead of webhook-instant.

---

## Deployment steps (mirrors PR #304's phase structure)

```bash
# On the VPS, alongside the existing /opt/deepiri/deepiri-platform checkout
mkdir -p /opt/deepiri && cd /opt/deepiri
git clone git@github.com:Team-Deepiri/deepiri-boardman.git
cd deepiri-boardman

# Secrets (separate from the platform's bundle — see "Secrets on the box" above)
cp .env.production.example .env
nano .env   # fill PLAKY_API_KEY, GITHUB_PAT, GITHUB_WEBHOOK_SECRET, DATABASE_URL, LLM_PROVIDER + key
chmod 600 .env

# Bring up Boardman's own stack (postgres + boardman + worker + nginx)
docker compose -f docker-compose.prod.yml up -d --build

# Apply migrations against the Postgres this compose file just started
docker compose -f docker-compose.prod.yml exec boardman alembic upgrade head

# Health check (internal — no public route configured yet at this step)
curl -sS http://127.0.0.1:8090/api/v1/health
```

Then: add the subdomain server block to the shared nginx (or stand up a second nginx
container bound to a different host port and front it with the platform's nginx as a
reverse-proxy target), issue the Let's Encrypt cert for the chosen subdomain, and register
the GitHub webhook URL.

---

## Bottom line

- **Fits comfortably.** Combined footprint (~250-300 MB for Boardman + ~316-440 MB for the
  platform stack) is well under 1 GB against an 8 GB box with ~7.5 GB of headroom already
  demonstrated under the platform's own load testing.
- **CPU, not memory, is the box's real ceiling** — Boardman's I/O-bound webhook/API workload
  doesn't add meaningful CPU pressure, but it hasn't been measured running *simultaneously*
  with the platform stack under load; worth one real `docker stats` pass after both are live.
- **The actual work is routing/secrets/ops, not capacity**: pick subdomain vs. path routing,
  pick separate vs. shared Postgres (separate recommended), keep Boardman's secrets in their
  own file, and decide whether the webhook goes live immediately or Boardman bridges on
  `TESTING_LIVE_PLAKY` polling until DNS/TLS are ready.
