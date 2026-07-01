# Boardman — Hosting Requirements (one-pager for the support session)

Purpose: settle where Boardman runs and how it's reached, in one sitting.

---

## What Boardman is (and why it needs a home)

A small, always-on backend service. It receives GitHub webhooks and updates Plaky. It is stateful:
it runs a FastAPI server plus a background worker and keeps a small SQLite database and an on-disk
cache. It must live on a persistent, always-on host — it cannot run as a stateless function.

---

## What it needs to run

- Host: one small Linux VPS or container slot. Runs via Docker Compose (API + worker). Modest specs;
  it is I/O-bound, not compute-heavy. No GPU.
- Public HTTPS URL: a subdomain (e.g. `boardman.deepiri…`) pointing at the host, so GitHub can deliver
  webhooks. Cloudflare in front for DNS/TLS is fine.
- Persistent volume: for `boardman.db` and the placement-catalog cache.
- GitHub auth: an org-owned token (read-only) — see below.
- Plaky API key.
- Webhook secret (HMAC) shared between GitHub and Boardman.
- Optional hosted LLM key: only sharpens fuzzy matching. Can be left off at launch.

---

## GitHub auth — answering "service account vs norozo vs org PAT"

Boardman's GitHub access is read-only. It needs: read on repos (issues, PRs, reviews, comments,
contents) and `read:org` (to read the `Team-Deepiri/support-team` roster, which drives QA assignment).
It never writes to GitHub.

- An org-owned PAT is enough for wave one. "Service account" just meant "not a personal token that
  dies when someone leaves" — an org-owned fine-grained PAT satisfies that.
- Hard requirement: the token must include `read:org`, or QA auto-assignment can't see the team.
- Reusing norozo's credential: possible but not recommended — different service, may lack `read:org`,
  couples rotation/debugging. Boardman having its own token (not its own account) is cleaner.
- GitHub App is the better long-term option (org-owned, fine-grained, auto-rotating tokens, one
  org-wide install, higher rate limits) — but it is an auth upgrade, not a hosting solution.

---

## Clearing up the three options (they solve different problems)

- VPS / container: where Boardman lives. Required for production.
- Cloudflare Worker: cannot be Boardman (stateless, no disk/DB). In this repo it is only an optional
  thin proxy/fallback for the QA-assignment route. Fine in front for TLS/routing; not a host.
- GitHub App: an auth/identity choice. Still delivers webhooks to a URL you host — does not remove the
  need for the VPS or the public address.

Net: VPS hosts it, a subdomain gives it a public URL, PAT or GitHub App handles auth.

---

## Recommended minimal setup (a default to approve or adjust)

1. Small Linux VPS, Docker Compose (`docker-compose.prod.yml`), worker-only mode.
2. Subdomain + TLS (Cloudflare) → the host's `:8090`.
3. Org PAT with `read:org` (move to a GitHub App later if desired).
4. One org-level webhook → `https://<subdomain>/api/v1/webhooks/github`.
5. Pilot first: temporary tunnel + one repo's webhook, validate on a real PR, then go org-wide.

---

## Decisions to make in the session

- Who owns/pays for the VPS, and which provider?
- Subdomain name + who manages DNS/TLS?
- PAT now vs GitHub App — and who creates the org-owned credential (with `read:org`)?
- Pilot repo to start with.
