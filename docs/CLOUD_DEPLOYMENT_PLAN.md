# Boardman cloud deployment plan (final)

Supersedes the earlier speculative version of this doc. This one is based on actually
SSH-ing into the real target VPS (read-only checks only — no changes made) and on
decisions made and tested this session: **UI deployed alongside the API, OpenRouter
free-tier for inference, reuse the nginx already running there.**

**Update:** the earlier "no chat UI on this box" call was reversed — `boardman-ui` now
ships with the API/worker in this plan (see "What actually gets deployed" below).

**Deploy is currently BLOCKED**: a teammate is concurrently deploying `deepiri-platform` /
`deepiri-web-frontend` to this same VPS. Do not touch the box until that's confirmed done.

---

## The actual box (measured live, not estimated)

`159.195.234.19` — already running the `deepiri-platform` stack.

| | |
|---|---|
| CPU | 4 vCore, AMD EPYC-Genoa |
| RAM | 7.8 GB total, **6.3 GB available** right now |
| Disk | 251 GB, 9.2 GB used — **232 GB free** |
| OS | Debian 13 (trixie) |
| Docker | 29.7.2 + Compose v5.5.0, already installed |

**Already running** (13 containers, measured combined RAM: **~215 MB** — about 3% of the box): nginx, platform-frontend, api-gateway, auth-service, jobs, external-bridge-service, registry, postgres-platform, redis, certbot, pg-backup-offsite, lyback, a proxy container on `:8888`.

**Ports 80/443/22/8888 are taken** (by the above). **8090/8091/5433 are free.** `/opt/deepiri/deepiri-platform` already exists; Boardman goes in as a sibling: `/opt/deepiri/deepiri-boardman`.

**Conclusion: fits easily.** Boardman's own measured footprint (~110 MB API process) plus a small Postgres instance is well under 1 GB against 6.3 GB free.

---

## Decisions made this session

1. **UI deployed too.** `boardman-ui` ships alongside the API/worker on this box (reversing an earlier "backend-only" call) — nginx serves the built static UI and proxies `/api/` to `boardman:8090`.
2. **No local inference.** No GPU on this box (confirmed: the only display device is a virtual/QEMU stub, no `nvidia-smi`). Running even a small local model here would be slow (CPU-only) and would compete with the platform stack's RAM. This also matches the platform's own documented policy of keeping heavy AI off this class of VPS.
3. **Inference = OpenRouter, free tier, by default.** `LLM_PROVIDER=openrouter`, and the codebase's own default model (when `LLM_MODEL` is unset) is now `minimax/minimax-m3:free` — **live-verified this session**: it correctly drove Boardman's tool-calling agent to create a real Plaky task and read a real GitHub repo. If someone needs a stronger model temporarily, they don't need a deploy change — see **bring-your-own-key** below.
4. **Bring-your-own-key (BYOK).** A chat session can supply its own provider API key instead of the shared free-tier one. Encrypted at rest (Fernet, server-side secret `BYOK_ENCRYPTION_KEY`), time-limited (24h default), never echoed back in any response. Off entirely unless `BYOK_ENCRYPTION_KEY` is set. See `boardman/security/byok.py`.
5. **Route through the existing nginx**, not a second one. `deploy/nginx/boardman.deepiri.com.conf` is a server block to add to the nginx container already running on this box — serves the built `boardman-ui` static assets and proxies `/api/` to `boardman:8090`.
6. **Own Postgres instance**, separate from the platform's `postgres-platform` (unchanged from the earlier version of this plan — trivial resource cost, keeps failure domains independent).

---

## What actually gets deployed

| Component | Why | Resource cost |
|---|---|---|
| `boardman` (API) | Webhook receiver + REST — the only piece that must be internet-reachable | ~110 MB RAM (measured) |
| `boardman-worker` | Background/deferred job processing | ~80-110 MB RAM (same import weight, no HTTP layer) |
| Postgres (own instance) | `DATABASE_URL` for Boardman only | ~30-50 MB RAM |
| `boardman-ui` (static build) | Chat frontend, served via existing nginx | negligible (static files, no separate process) |
| ~~Ollama~~ | **Not deployed** — no GPU, OpenRouter instead | — |

Total: **~250-300 MB**, against 6.3 GB currently free.

---

## Steps

```bash
# On the VPS, alongside the existing /opt/deepiri/deepiri-platform
cd /opt/deepiri
git clone git@github.com:Team-Deepiri/deepiri-boardman.git
cd deepiri-boardman

cp .env.production.example .env
# Fill in: PLAKY_API_KEY, GITHUB_PAT, GITHUB_WEBHOOK_SECRET, DATABASE_URL (own postgres),
# LLM_PROVIDER=openrouter, OPENROUTER_API_KEY (free-tier key),
# BYOK_ENCRYPTION_KEY (generate one: `openssl rand -hex 32`)
chmod 600 .env

# Bring up boardman + boardman-worker + its own postgres (docker-compose.prod.yml
# already runs a postgres service — see the Postgres migration section below)
docker compose -f docker-compose.prod.yml up -d --build

docker compose -f docker-compose.prod.yml exec boardman alembic upgrade head

# Health check (internal, no public route yet)
curl -sS http://127.0.0.1:8090/api/v1/health
```

Then:
1. **Cloudflare**: add a DNS record for `boardman.deepiri.com` → this VPS's IP.
2. **nginx**: add `deploy/nginx/boardman.deepiri.com.conf`'s server block to the nginx config already running on the box (see the comments in that file for the exact wiring — shared docker network + a dedicated Let's Encrypt cert for the new hostname).
3. **GitHub**: register the webhook at `https://boardman.deepiri.com/api/v1/webhooks/github` with the `GITHUB_WEBHOOK_SECRET` from step above.

---

## Postgres migration (already built and tested)

`DATABASE_URL=postgresql+asyncpg://...` — SQLite serializes writes to one connection, which is real contention once the API and worker both write concurrently. The Postgres path is fully migrated and verified (see the Postgres migration PR): all migrations apply cleanly, 10 concurrent writers × 20 rows commit in ~0.1s.

---

## Deepiri-web-frontend Tools page entry

A "Boardman" tile on the platform's Tools page (linking to `https://boardman.deepiri.com`) was requested — that's a change to a **different repo** (`deepiri-web-frontend`), out of scope for this repo/plan. Flagging it here so it isn't lost; needs a separate pass once someone opens that repo.

---

## What's NOT verified end-to-end yet

- The nginx wiring above is a config file, not something applied to the live box — I did not touch the running nginx (read-only checks only, per what was actually authorized this session).
- The `deepiri-web-frontend` Tools page entry — a `boardman` tile was added in a separate PR against that repo (open, points to `https://boardman.deepiri.com`).
- Live proof used the *sandbox's* credentials/board, not a real deployment on `159.195.234.19` — the box fits and the code works, but nobody has actually run `docker compose up` there yet.

---

## Immediate next steps (in order)

1. **Wait for teammate's concurrent deploy to finish** on `159.195.234.19` (platform + web-frontend). Confirm with them before touching the box.
2. **Merge the routing-placement fix** (`boardman/services/scan_handler.py`) — a real bug where `run_repo_scan` discarded a correctly-resolved board/group for any repo whose Plaky routing came from live discovery (`discovered:*`) rather than explicit `repos.yml`, silently failing every create. Fixed and regression-tested (`tests/test_scan_placement.py`); verified live against `Team-Deepiri/deepiri-axiom`.
3. **Org-wide Plaky task generation** (in progress this session): dry-run pass across all 46 repos with resolvable Plaky routing completed (370 tasks proposed, quality verified); live pass re-running now with the placement fix applied. 17 of the 66 active repos have no matching Plaky group and cannot be auto-created (Plaky's public API has no board/group-create endpoint) — a human needs to add a group named after each repo slug on the correct categorical board first; the list is logged as `plaky placement: no Plaky group matches repo slug '<slug>'` warnings during any scan run.
4. **Once the teammate's deploy is clear**: `git clone` + `docker compose -f docker-compose.prod.yml up -d --build` per the Steps section above, including the UI, then wire nginx + Cloudflare DNS + the GitHub webhook.
5. **Smoke test** PR↔Plaky linking and QA assignment against real open PRs org-wide once deployed.
6. **Boardman UI account creation** — passcode-gated signup was raised (passcode value tracked outside this doc); recommendation is to reuse `deepiri-auth-service` rather than build separate auth, with the passcode as an invite-gate at registration. Not yet built — needs explicit go-ahead.
