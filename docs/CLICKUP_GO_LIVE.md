# ClickUp go-live checklist

ClickUp is Deepiri's task system. Boardman has been tested against a real ClickUp workspace
(task created from a GitHub issue, then moved through PR opened, QA review, approval, QA
rejection and merge, each status read back from ClickUp). What has **not** been tested is real
GitHub deliveries into the deployed service, so the last step below is the real proof.

## 1. In ClickUp (the team's workspace)

1. Create the list Boardman writes to (a sandbox list first is fine).
2. Give it these statuses, spelled exactly (case does not matter, spelling and spaces do):

   `to do`, `assigned`, `in progress`, `needs qa`, `in qa`, `qa verified`, `qa rejected`, `complete`

   ClickUp's API cannot create statuses; add them in the list's *Edit statuses* screen.
3. Create an API token for Boardman. Use a service account if the workspace has one, so
   tasks do not depend on one person's login. The token goes in `.env` only, never in git or chat.
4. Find the ids: workspace (team) id from `https://app.clickup.com/<id>/...`, and the list id
   from the list's URL.

## 2. On the server (`.env` next to `docker-compose.prod.yml`)

```
TASK_PROVIDER=clickup
CLICKUP_API_TOKEN=pk_...
CLICKUP_TEAM_ID=<workspace id>
CLICKUP_DEFAULT_LIST_ID=<list id>
# status names, only if they differ from the defaults:
# CLICKUP_STATUS_NEEDS_QA=needs qa  ...see .env.production.example
```

Per repo, in `repos.yml`:

```yaml
repos:
  Team-Deepiri/<repo>:
    category: devtools
    clickup_list_id: "<list id>"
```

Leaving `TASK_PROVIDER` unset keeps Plaky, so set it deliberately in the same deploy.

## 3. Check before sending any traffic

```
boardman doctor                          # token, workspace and every list/status name
boardman clickup-inventory --list-id <list id>
```

`doctor` prints one line per problem, for example `List 123 has no status 'qa rejected'`. The
service also logs these at startup as `ClickUp config: ...` warnings. Fix them before going on:
a missing status does not crash anything, the task just stays where it was.

## 4. Real GitHub delivery test (use a test repo)

1. Repo *Settings > Webhooks*: payload `https://boardman.deepiri.com/api/v1/webhooks/github`,
   content type JSON, secret = `GITHUB_WEBHOOK_SECRET`, events: issues, pull requests, pull
   request reviews, issue comments.
2. Open an issue with an assignee: a task appears in the list, status `assigned`.
3. Open a PR with `Fixes #N`: the task moves to `needs qa` and gets a comment.
4. A second person reviews it: `in qa`, then approve: `qa verified`.
5. Merge: `complete`.

Then `docs/CLICKUP_LIVE_CHECKLIST.md` for the rest. `scripts/clickup_live_trace.py` runs steps
2 to 5 with signed test webhooks if you want a repeatable check.

## 5. Afterwards

Once ClickUp has run in production for a while, Plaky can be removed from the code and the
default switched to ClickUp. Until then the default stays Plaky so an unconfigured deploy cannot
point at an unset ClickUp.
