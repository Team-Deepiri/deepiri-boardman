# Boardman — Deploy & Test Runbook

Follow top to bottom. The PR is reviewed; this covers merge → deploy → verify.
Boardman runs worker-only in production (webhook automation, no chat/UI).

---

## 1. Merge the code

Merge the approved PR (`ali_f/feat/qa-automation-plus-discovery`) into `dev`, then `dev` → `main`
when ready to deploy. Deploy from the branch you merged to.

---

## 2. Provision the host

- A small Linux VPS (any provider). Note its public IP.
- DNS: add an A record `boardman.deepiri.com` → VPS IP, with TLS (Cloudflare in front is fine).

---

## 3. Create the GitHub token

GitHub → Settings → Developer settings → Fine-grained PAT, scoped to the Team-Deepiri repos:
- Contents: Read
- Issues: Read
- Pull requests: Read
- Organization → Members: Read  (`read:org` — required for QA assignment)

Copy the token.

---

## 4. Install Docker on the VPS

```bash
ssh <user>@<vps-ip>
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # then log out and back in
```

---

## 5. Clone the repo

```bash
git clone https://github.com/Team-Deepiri/deepiri-boardman.git
cd deepiri-boardman
git checkout main        # or the branch you deployed
```

---

## 6. Configure the environment

```bash
cp .env.production.example .env
nano .env
```

Set these (the rest of the file is already correct for production):

```dotenv
PLAKY_API_KEY=<plaky key>
GITHUB_PAT=<token from step 3>
GITHUB_WEBHOOK_SECRET=<run: openssl rand -hex 32>
WORKER_INTERNAL_SECRET=<run: openssl rand -hex 32>
BOARDMAN_SECRETS_ROTATED=true
BOARDMAN_ENABLE_AGENT_API=false
GITHUB_ORG=Team-Deepiri
BOARDMAN_PUBLIC_URL=https://boardman.deepiri.com
```

Keep the `GITHUB_WEBHOOK_SECRET` value handy — you reuse it in step 9.

---

## 7. Start the stack

```bash
# pre-create the DB file so Docker doesn't turn it into a directory
test -d boardman.db && rm -rf boardman.db
: > boardman.db && chmod 600 boardman.db

docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
```

---

## 8. Verify it's healthy

```bash
curl -fsS https://boardman.deepiri.com/api/v1/health          # expect {"ok":true,...}
BOARDMAN_COMPOSE_FILE=docker-compose.prod.yml bash scripts/deploy_smoke.sh
```

Check logs if anything looks off:
```bash
docker compose -f docker-compose.prod.yml logs --tail=100 boardman
```

---

## 9. Connect ONE test repo (pilot)

On a low-risk repo → Settings → Webhooks → Add webhook:
- Payload URL: `https://boardman.deepiri.com/api/v1/webhooks/github`
- Content type: `application/json`
- Secret: your `GITHUB_WEBHOOK_SECRET`
- Events (select individual): Issues, Pull requests, Pull request reviews,
  Pull request review comments, Issue comments

Save. GitHub sends a `ping` — it should show a green check (HTTP 200).

---

## 10. End-to-end test

On that repo, do these and watch the deepiri-boardman Plaky board react:

1. Open an issue → a task is created and a QA is auto-assigned.
2. Open a PR from a `feat/...` branch with `Fixes #<issue>` → task shows Needs QA, Type = Feature,
   and the developer filled in as Assignee.
3. The assigned QA comments → In QA. Requests changes → QA Rejected. Push a commit → In Progress.
4. The assigned QA approves → QA Verified.
5. Merge the PR → Completed.

If a step doesn't fire, `docker compose logs boardman` prints the reason for every webhook.

---

## 11. Roll out

Once the pilot works, add one org-level webhook (Team-Deepiri → Settings → Webhooks) with the same
URL, secret, and events, covering all repos. Spot-check the auto-assigned QA roster, then enable in
waves.

---

## Notes

- QA is assigned from the `Team-Deepiri/support-team` GitHub team, matched to Plaky users
  automatically. The `read:org` scope on the token is what makes this work.
- Merged PRs move the task to "Completed" (leave `PLAKY_PR_MERGE_STATUS` empty — already default).
- To rotate a secret later: update `.env`, then
  `docker compose -f docker-compose.prod.yml up -d --force-recreate boardman boardman-worker`.
