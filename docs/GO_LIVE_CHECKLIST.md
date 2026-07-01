# Boardman — Go-Live Checklist

A clean, ordered path to production. Each step is "do this, then verify." The full reference is
[DEPLOYMENT.md](./DEPLOYMENT.md); this is the short version.

Production runs **worker-only**: the GitHub→Plaky webhook automation, no chat/UI.

---

## Phase 1 — Accounts & secrets (do once, off-server)

1. **Plaky service account** — create a dedicated Plaky account (not a personal one), add it to the
   workspace, and generate its API key. This becomes `PLAKY_API_KEY`.
2. **GitHub service PAT** — a token with `repo` (read) + `read:org` scopes (read:org is needed so
   boardman can read the `Team-Deepiri/support-team` roster for QA assignment). This is `GITHUB_PAT`.
3. **Generate three secrets** (run three times):
   ```bash
   openssl rand -hex 32
   ```
   → `GITHUB_WEBHOOK_SECRET`, `WORKER_INTERNAL_SECRET`, `ROUTE_SECRET`.

✅ You now have: Plaky key, GitHub PAT, and three random secrets.

---

## Phase 2 — Server setup

4. **Provision** an Ubuntu VPS with a domain/HTTPS in front (Cloudflare or a TLS reverse proxy).
5. **Install Docker** and clone the repo:
   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker "$USER"          # log out/in after this
   git clone https://github.com/Team-Deepiri/deepiri-boardman.git
   cd deepiri-boardman
   ```

✅ Docker runs and the repo is on the server.

---

## Phase 3 — Configure

6. **Create the env file** and fill it in:
   ```bash
   cp .env.production.example .env
   nano .env
   ```
   Set these (the rest of the file is already production-correct):
   ```dotenv
   PLAKY_API_KEY=<plaky service key>
   GITHUB_PAT=<github service PAT>
   GITHUB_WEBHOOK_SECRET=<random hex #1>
   WORKER_INTERNAL_SECRET=<random hex #2>
   ROUTE_SECRET=<random hex #3>          # only if you use the Cloudflare QA worker; else leave blank
   BOARDMAN_SECRETS_ROTATED=true
   BOARDMAN_TARGET_ENV=vps
   BOARDMAN_PUBLIC_URL=https://<your-boardman-host>
   LLM_PROVIDER=openai                    # or gemini; provide the matching API key
   OPENAI_API_KEY=<key>                   # optional — only powers fuzzy-match boosting
   ```
   > `BOARDMAN_ENABLE_AGENT_API=false`, `GITHUB_ORG=Team-Deepiri`, and an empty
   > `PLAKY_PR_MERGE_STATUS` (→ "Completed" on merge) are already set in the template.

7. **(Optional) QA roster corrections** — boardman auto-builds the QA roster from the
   `Team-Deepiri/support-team` GitHub team and auto-matches each member to their Plaky user. You only
   need to touch `team_assignments.yml` if a match is wrong or a member's `qa_tier` needs setting —
   add a `member_overrides` entry keyed by their GitHub login (see the comments in that file).

✅ `.env` is filled; the roster is automatic.

---

## Phase 4 — Deploy

8. **Pre-create the database file** (prevents a Docker bind-mount gotcha), then start:
   ```bash
   test -d boardman.db && rm -rf boardman.db
   : > boardman.db && chmod 600 boardman.db
   docker compose -f docker-compose.prod.yml up -d --build
   docker compose -f docker-compose.prod.yml ps
   ```
9. **Verify it's healthy:**
   ```bash
   curl -fsS http://localhost:8090/api/v1/health         # expect {"ok":true,...}
   BOARDMAN_COMPOSE_FILE=docker-compose.prod.yml bash scripts/deploy_smoke.sh
   ```

✅ Services are up; health + webhook-ping smoke pass.

---

## Phase 5 — Connect GitHub (start with ONE repo)

10. On a low-risk test repo → **Settings → Webhooks → Add webhook**:
    - **Payload URL:** `https://<your-boardman-host>/api/v1/webhooks/github`
    - **Content type:** `application/json`
    - **Secret:** your `GITHUB_WEBHOOK_SECRET`
    - **Events (choose individual events):** Issues, Pull requests, Pull request reviews,
      Pull request review comments, Issue comments
    > "Pull requests" must include the **synchronize** event (it's included by default) — it drives
    > the resume-to-In-Progress transition.

✅ GitHub shows a green check on the webhook's initial `ping` delivery.

---

## Phase 6 — End-to-end smoke (on the test repo)

11. Open an issue → confirm a Plaky task appears (with a QA auto-assigned).
12. Open a PR with `Fixes #<issue>` on a `feat/...` branch → confirm the task shows **Needs QA**,
    **Type = Feature**, and the **Assignee** filled in.
13. Have the assigned QA comment, then request changes, then approve → watch the task move
    **In QA → QA Rejected → … → QA Verified**.
14. Merge the PR → task goes to **Completed**.

✅ Record the result (see the handoff block in DEPLOYMENT.md).

---

## Phase 7 — Roll out

15. Add the same webhook to the rest of the org's repos (or configure it org-wide).
16. **Board placement** (ongoing): as each repo gets its group on a category board, set that repo's
    `plaky_board_id` in `repos.yml` and redeploy. Repos without a placement route to the main board.

---

## One-glance "is it done?" gate

```bash
poetry run boardman readiness        # locally, or on the server before go-live
```
Everything except live runtime checks should be PASS once Phase 3 secrets are set with
`BOARDMAN_SECRETS_ROTATED=true`.
