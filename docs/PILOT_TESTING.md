# Pilot Testing — Run Boardman Against Real GitHub

How to point a real GitHub repo at your local Boardman and watch it drive Plaky. This is the
pilot path the team agreed on: a temporary repo-level webhook + a temporary public tunnel. No
production host required.

Test board: the Plaky board titled **deepiri-boardman** (board `269031`, new column design).
`repos.yml` already routes the `deepiri-boardman` repo there.

---

## Option A — No GitHub needed (fastest)

Drives the whole lifecycle with simulated (signed) webhooks and prints each status change:

```bash
poetry run python -m boardman.main                 # terminal 1
poetry run python scripts/edge_cases_live.py       # terminal 2
```

You should see 15/15 checks pass: the full status flow plus edge cases (idempotency, bad
signature, draft-skip, no-match). Use this for quick regression checks.

---

## Option B — Real GitHub PR (true end-to-end)

### 1. Start Boardman locally
```bash
poetry run python -m boardman.main      # http://localhost:8090
```

### 2. Open a public tunnel to port 8090
Pick whichever tool you have:
```bash
# Cloudflare (no account needed for quick tunnels)
cloudflared tunnel --url http://localhost:8090

# or ngrok
ngrok http 8090
```
Copy the HTTPS URL it gives you, e.g. `https://abc-123.trycloudflare.com`.

### 3. Set a webhook secret
In your local `.env`, set a real value (any random string is fine for pilot):
```dotenv
GITHUB_WEBHOOK_SECRET=pilot-secret-change-me
```
Restart Boardman so it picks it up.

### 4. Add a repo-level webhook
On a low-risk test repo → **Settings → Webhooks → Add webhook**:
- **Payload URL:** `https://<your-tunnel>/api/v1/webhooks/github`
- **Content type:** `application/json`
- **Secret:** the same `GITHUB_WEBHOOK_SECRET`
- **Events → "Let me select individual events":** Issues, Pull requests, Pull request reviews,
  Pull request review comments, Issue comments

GitHub immediately sends a `ping`; it should show a green check (200).

### 5. Drive a PR and watch Plaky
1. **Open an issue** → a task appears on the deepiri-boardman board, QA auto-assigned.
2. **Open a PR** from a `feat/...` branch with `Fixes #<issue>` in the body → task shows
   **Needs QA**, **Type = Feature**, and **Assignee** filled in.
3. The **assigned QA comments** on the PR → **In QA**.
4. The QA **requests changes** → **QA Rejected**; **push a commit** → **In Progress**.
5. Comment **"pause"** → **Paused**; **@-mention the QA** → **Needs QA Again**.
6. The QA **approves** → **QA Verified**; **merge** → **Completed**.

> The assigned-QA steps only fire for the GitHub user Boardman picked as QA (from the
> `Team-Deepiri/support-team` roster). Check the task's "QA Engineer Assigned" field to see who,
> and have that person (or a test account with that login) perform the QA actions — or use Option A
> which controls the actor for you.

### 6. Watch what Boardman is doing
```bash
# server log (already streaming if you ran it in the foreground)
# every webhook prints a 200 line and the handler result
```

### 7. Clean up
Delete the temporary webhook and stop the tunnel when done. Delete the `[edge-test]` tasks from
the board.

---

## What to look for

| You do on GitHub | Boardman should | If not |
|---|---|---|
| Open issue | Create task + assign QA | Check repo routes to a board with the QA schema; check PAT can read the support team |
| Open PR `Fixes #N` | Link, set Type, fill Assignee, → Needs QA | Confirm the issue's task exists; check server log for the link result |
| Assigned QA comments | → In QA | Make sure the commenter is the assigned QA (see the QA field) |
| Merge | → Completed | Confirm `PLAKY_PR_MERGE_STATUS` is empty (resolves "Completed") |

If a webhook returns non-200, the server log prints the reason (bad signature, unsupported event,
no linked task, etc.).
