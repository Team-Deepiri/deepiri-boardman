# Migrating Boardman off a personal PAT onto a GitHub App

## Why

Boardman currently authenticates to GitHub with `GITHUB_PAT` — a personal access token
tied to a real human account (`jrb00013`). Every comment Boardman posts (QA-assignment
notices, PR↔Plaky link notices, issue-text mirrors) shows up as **that person**, not as a
bot. That's misleading (a QA reviewer gets pinged by "the founder," not "Boardman"), and
it ties an automation's access — and its blast radius if the token ever leaks — to one
person's GitHub identity rather than to the app itself.

A **GitHub App** gives Boardman its own bot identity (comments post as
`boardman[bot]`), its own scoped permissions (independent of any human's account
permissions), and short-lived, auto-rotating tokens instead of one long-lived PAT.
`GITHUB_AUTH_MODE=github_app` already exists as a settings value
(`boardman/readiness.py`) — nothing implements it yet. This doc is the plan to close
that gap.

---

## Part 1 — Create the GitHub App (one-time, org owner action)

1. Go to `github.com/organizations/Team-Deepiri/settings/apps/new` (org owner permission
   required — this cannot be done from a personal account settings page).
2. **GitHub App name**: `Boardman` (or `deepiri-boardman` if that's taken).
3. **Homepage URL**: `https://boardman.deepiri.com`.
4. **Webhook**:
   - Active: yes
   - Webhook URL: `https://boardman.deepiri.com/api/v1/webhooks/github` (same endpoint
     Boardman already serves — no new route needed)
   - Webhook secret: generate a new one (`openssl rand -hex 32`), separate from the
     existing org-webhook secret. Keeping them separate means retiring the old
     org-level webhook later doesn't require touching this one.
5. **Permissions** (repository permissions — set these, leave everything else "No access"):
   | Permission | Access | Why |
   |---|---|---|
   | Issues | Read and write | Comments, QA-assignment notices, issue sync |
   | Pull requests | Read and write | Comments, reviewer requests, PR sync |
   | Contents | Read-only | DIRECTION.md fetch, repo scanning |
   | Metadata | Read-only | Required by every GitHub App, always on |
   | Deployments | Read-only | The `deployment_status` → "Deployed" status trigger |

   Organization permissions: **Members → Read-only** (needed to resolve the
   `Team-Deepiri/support-team` roster the same way the PAT does today).

6. **Subscribe to events**: `pull_request`, `pull_request_review`,
   `pull_request_review_comment`, `issue_comment`, `issues`, `deployment_status` — the
   same list already registered on the org-level webhook
   (`gh api orgs/Team-Deepiri/hooks`).
7. **Where can this GitHub App be installed?**: "Only on this account" (Team-Deepiri) —
   never "Any account."
8. Create the app. On the app's settings page:
   - Note the **App ID** (top of the page).
   - Under **Private keys**, click **Generate a private key** — downloads a `.pem`
     file. This is the credential; treat it like the PAT is treated today (never
     committed, never pasted in chat).

## Part 2 — Install the app (one-time, org owner action)

1. On the app's settings page, click **Install App**.
2. Choose **Team-Deepiri**.
3. Repository access: **All repositories** — matches the same reasoning already applied
   to the PAT (a bot reconciling across 60+ repos should not need a manual step every
   time a new repo is created).
4. After installing, note the **Installation ID** from the URL
   (`github.com/organizations/Team-Deepiri/settings/installations/<ID>`) or via
   `gh api /orgs/Team-Deepiri/installations`.

At the end of Part 1 + 2 you have three values: **App ID**, **Installation ID**, and the
**private key** (`.pem` contents).

---

## Part 3 — What changes in Boardman (code, not yet built)

### New settings (`boardman/settings.py`)

```python
github_app_id: str = ""
github_app_installation_id: str = ""
github_app_private_key: str = ""  # PEM contents, not a file path (matches how
                                    # BYOK_ENCRYPTION_KEY etc. are already passed in)
github_app_webhook_secret: str = ""  # separate from github_webhook_secret
```

### New module: `boardman/github/app_auth.py`

A GitHub App doesn't get one static token. It signs a short-lived JWT with the private
key, exchanges that JWT for an **installation access token** (expires in 1 hour), and
must refresh it before expiry. This needs:

```python
async def get_installation_token() -> str:
    """Returns a cached, auto-refreshed installation access token.

    Mints a JWT signed with GITHUB_APP_PRIVATE_KEY (RS256, iss=GITHUB_APP_ID, 10-minute
    expiry per GitHub's own limit), POSTs it to
    /app/installations/{GITHUB_APP_INSTALLATION_ID}/access_tokens, and caches the
    resulting token in-process until ~5 minutes before its own expiry (GitHub's tokens
    are valid 1 hour). A cache miss or near-expiry mints a new one.
    """
```

Implementation notes:
- JWT signing needs `PyJWT` with the `cryptography` extra (already a dependency —
  `boardman/security/byok.py` uses `cryptography` already) — `pyjwt[crypto]` in
  `pyproject.toml`.
- Cache this the same way `boardman/llm/factory.py` caches chat models: a
  module-level dict keyed by installation ID, guarded so concurrent requests
  don't all mint a fresh token at once (an `asyncio.Lock` around the mint call is
  enough at this scale).

### The 25-file problem

`settings.github_pat` is read directly in 25 files (`grep -rl settings.github_pat
boardman/` at time of writing). Every one of them builds its own
`{"Authorization": f"Bearer {settings.github_pat}"}` header inline. A real migration
needs **one seam**, not 25 edits:

```python
# boardman/github/auth.py (new)
async def github_auth_header() -> dict[str, str]:
    """The one place that decides PAT vs GitHub App, honoring GITHUB_AUTH_MODE."""
    mode = (settings.github_auth_mode or "pat").strip().lower()
    if mode in ("github_app", "both"):
        token = await get_installation_token()
        return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    return {"Authorization": f"Bearer {settings.github_pat}", "Accept": "application/vnd.github+json"}
```

Then every one of those 25 files' inline header-building gets replaced with
`await github_auth_header()`. This is mechanical but touches a lot of surface area —
budget it as its own PR, reviewed carefully, not bundled with anything else. Grep
target list (from this repo, current state):

```
boardman/sqlite_worker.py            boardman/github/repo_hotspots.py
boardman/main.py                     boardman/github/team_roster.py
boardman/agent/tools/github_tools.py boardman/services/github_poller.py
boardman/repos_config.py             boardman/services/reconcile.py
boardman/agent/org_roster.py         boardman/github/repo_metadata.py
boardman/planning/huddle/context_direction.py   boardman/github/repo_matcher.py
boardman/github/pr_actions.py        boardman/services/pr_review_handler.py
boardman/github/qa_contribution_profile.py      boardman/github/code_search.py
boardman/routes/repos.py             boardman/services/scan_handler.py
boardman/planning/huddle/context_github.py      boardman/services/repo_knowledge.py
boardman/github/repo_fetch.py        boardman/cli/commands.py
boardman/github/pr_review_context.py boardman/assignment/qa_picker.py
boardman/github/org_repos.py
```

### Webhook verification

`boardman/routes/github_events.py` verifies inbound webhook signatures against
`GITHUB_WEBHOOK_SECRET`. A GitHub App webhook is signed with **its own** secret
(`github_app_webhook_secret` above), separate from the org-level webhook's secret. The
verification function needs to accept either secret depending on which delivery
mechanism sent the request (or, cleaner: once the App is fully cut over, retire the
org-level webhook entirely and there's only one secret again).

---

## Part 4 — Rollout plan (avoid a big-bang cutover)

1. **Land the code with `GITHUB_AUTH_MODE=pat` as the default.** No behavior change for
   anyone until the setting is flipped. This is the safe PR to merge and deploy first.
2. **Add the three GitHub App secrets to `deepiri-boardman`'s GitHub Actions secrets**
   (`BOARDMAN_APP_ID`, `BOARDMAN_APP_INSTALLATION_ID`, `BOARDMAN_APP_PRIVATE_KEY`,
   `BOARDMAN_APP_WEBHOOK_SECRET`) via `gh secret set`, same pattern as the existing six
   app secrets — never pasted through a chat transcript.
3. **Wire them into `.github/workflows/deploy.yml`'s `.env` assembly**, same
   quoted-heredoc + envsubst + fail-fast-validation pattern already used for the other
   six secrets.
4. **Set `GITHUB_AUTH_MODE=both` on staging/one test repo first.** `"both"` should mean:
   try the App token, fall back to the PAT on any auth failure — a safety net during
   cutover, not a permanent mode. Watch `boardman-worker` logs for auth errors.
5. **Confirm comments now show `boardman[bot]`** as the author on a real test PR.
6. **Cut over fully**: `GITHUB_AUTH_MODE=github_app`, remove `GITHUB_PAT` from the
   deployed `.env` (keep the GitHub Actions secret around for a rollback window, don't
   delete it immediately).
7. **Retire the org-level webhook** (`gh api orgs/Team-Deepiri/hooks/672599960` —
   delete) once the App's own webhook has been proven live for a few days, so there's
   exactly one delivery path, not two.
8. **Revoke/rotate the personal PAT** last, once nothing in the deployed `.env`
   references it.

## Part 5 — What does NOT change

- Plaky auth (`PLAKY_API_KEY`) is untouched — this is entirely a GitHub-side migration.
- The reconciliation, QA-picking, and PR-linking logic itself doesn't change at all —
  only how the HTTP requests to GitHub's API are authenticated.
- `boardman/github/team_roster.py`'s live roster fetch keeps working the same way,
  just authenticated differently.

## Estimated scope

- New module (`app_auth.py` + `auth.py` seam): small, well-isolated, easy to unit test
  (mock the JWT mint + token exchange, assert caching/refresh behavior).
- The 25-file header migration: mechanical, but wide — budget a full review pass, not a
  quick pass. Good candidate for `fresh-eyes-reviewer` or a dedicated review round given
  the number of files touched.
- GitHub App creation + installation (Parts 1–2): 15–20 minutes of org-owner-only
  clicking, not something I can do remotely (creating a GitHub App requires org owner
  web UI access, not an API token).
