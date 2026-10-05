# ClickUp live checklist

Everything in the ClickUp port is tested against a mocked ClickUp. Nothing has run against the real
API. Use this checklist the first time you have a token, in order. Stop at the first failure: later
steps depend on earlier ones.

## 0. Set up a throwaway workspace

Use a test Space with one List. The write tools create, comment on, tag and archive real tasks.

```bash
TASK_PROVIDER=clickup
CLICKUP_API_TOKEN=pk_...                 # ClickUp > Settings > Apps > API Token
CLICKUP_TEAM_ID=...                      # recommended; otherwise the first workspace is used
CLICKUP_DEFAULT_LIST_ID=...              # the test list
```

## 1. Token, workspace and statuses

```bash
poetry run boardman doctor
poetry run boardman clickup-inventory --list-id "$CLICKUP_DEFAULT_LIST_ID"
```

- `doctor` should report the token and the workspace member count.
- `clickup-inventory` should list members and lists. The status check is the important part: every
  configured `CLICKUP_STATUS_*` name must exist in the list. Fix the settings (or the list) until it
  passes. ClickUp statuses are per list, so this is the most likely thing to be wrong.

## 2. Things the code assumes about the API (verify these)

| Assumption | Where | How to check |
|---|---|---|
| Personal tokens go in `Authorization` with no `Bearer` | `clickup/client.py` | step 1 working proves it |
| `POST /list/{id}/task` accepts `tags`, `status`, `assignees` and `priority` (1 to 4) | task creation | create a task (step 3) and look at it |
| `PUT /task/{id}` accepts `assignees: {add, rem}` and `archived` | updates, QA, archive | step 4 |
| `POST/DELETE /task/{id}/tag/{name}` add and remove a tag | `type:` tags | step 3 |
| `POST /task/{id}/field/{field_id}` with `{"value": {"add": [...], "rem": []}}` sets a **users** custom field, and `custom_fields[].value` reads back as a list of user objects | QA users field | only if you use `CLICKUP_QA_FIELD_ID`; step 5 |
| `GET /list/{id}/task?include_closed=...&subtasks=true&page=N` pages 100 at a time | listing, fuzzy matching | step 3 |
| `DELETE /task/{id}` answers 204 | orphan cleanup | step 6 |

## 3. Create and read back (API and CLI)

```bash
poetry run boardman create-task --title "Boardman smoke test" --board-id "$CLICKUP_DEFAULT_LIST_ID"
poetry run boardman list --board-id "$CLICKUP_DEFAULT_LIST_ID"
```

Check in ClickUp: the name, the priority, the status (it should follow ownership), and a
`type:feature` tag. Then create one with `--engineer-id <a member id>` and confirm the assignee and
that the status is the "assigned" one.

## 4. Update, comment, QA

```bash
poetry run boardman update-task --task-id <id> --status "<a real status>"
poetry run boardman link-pr --task-id <id> --pr-url https://github.com/<org>/<repo>/pull/1
poetry run boardman update-task --task-id <id> --qa-id <member id>
```

Without `CLICKUP_QA_FIELD_ID` the QA person is added as an extra assignee. With it, they go in the
users field instead (step 5).

## 5. QA users field (optional)

Create a custom field of type **Users** on the list, set `CLICKUP_QA_FIELD_ID` to its id, then repeat
the QA update. Confirm the field holds the person. Then open a PR flow (step 7) and confirm Boardman
sees the QA as already assigned and does not pick a second one. This read-back is the least certain
assumption in the port.

## 6. Cleanup and archive

With `PR_TASK_CLEANUP_ENABLED=true` and a short TTL, an orphan task created for an unmatched PR
should be deleted by the worker sweep. With `CLICKUP_ARCHIVE_COMPLETED_PRS=true`, a matched task
whose PR merged and whose status is the completed one should be archived (hidden, not deleted).

## 7. GitHub webhooks end to end (use a test repo)

Add a `clickup_list_id` for the repo in `repos.yml`, point a webhook at a dev Boardman, then:

1. Open an issue with an assignee: a task appears, tagged with the repo and `type:`, in the
   "assigned" status with the right owner.
2. Edit the issue title: the task is renamed. Change a label: the type tag changes.
3. Open a PR with `Fixes #N`: the task gets a notice comment, a QA, and moves to "needs QA".
4. Comment as the assigned QA: the task moves to "in QA". Approve: "approved". Request changes:
   "changes requested" (only from the assigned QA).
5. Push several commits after a verdict: "in progress", or "needs QA again" after more than five.
6. Merge the PR: the task moves to the completed status once, and only for a closing keyword in the
   description.
7. Close and reopen the issue: the task completes, then resumes the status it had.
8. Open a PR that names no issue: it fuzzy-matches an existing task, or (with `ambiguous_pr`
   enabled) gets its own.

## 8. Agent

Ask the chat agent to create two tasks, one a duplicate of an existing title. Expect one created and
one reported as "Already in ClickUp". Ask it to assign someone by plain name and to set QA.

If something here fails, the mocked tests encode what the code believes the API does. Fix the
assumption in `boardman/clickup/client.py` first, update the matching test in
`tests/test_clickup_client.py` or `tests/clickup_fake.py`, and everything above it follows.
