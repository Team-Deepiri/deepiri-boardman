# ClickUp provider

Boardman can use ClickUp (API v2) as its task provider, alongside Plaky. Plaky stays the default.

## Turn it on

```bash
TASK_PROVIDER=clickup
CLICKUP_API_TOKEN=pk_...          # personal token: ClickUp > Settings > Apps > API Token
CLICKUP_TEAM_ID=...               # optional: workspace id used for user listing
CLICKUP_DEFAULT_LIST_ID=...       # list new tasks are created in when none is given
```

The token is sent in the `Authorization` header as-is, with no `Bearer` prefix (OAuth tokens would need one).

## Vocabulary

| Plaky | ClickUp |
|---|---|
| board | list (`board_id` is a list id) |
| item / task | task |
| subtask | task with `parent` set |
| priority name | integer 1 urgent, 2 high, 3 normal, 4 low |

## What works through `TASK_PROVIDER`

`boardman/clickup/client.py` mirrors the task surface of `PlakyClient`, with the same `{"ok", "status", ...}` result shape:

`create_task`, `create_subtask`, `get_task`, `get_tasks`, `update_task_fields`, `add_comment`, `list_workspace_users`, `list_boards`.

These are wired through `boardman/task_provider.py` into `GET /tasks`, `GET /tasks/{id}`, `POST /tasks/{id}/link-pr` and the scan job's open-task lookup.

## What is still Plaky-only

Plaky has board schemas, custom fields and per-board placement that ClickUp does not model the same way. These still call `PlakyClient` directly and are not provider-neutral yet:

- GitHub webhook sync (issue and PR handlers), PR status transitions and QA assignment
- `PATCH /tasks/{id}` (`update_task_internal`)
- Agent tools, the planning and huddle code, and board-schema helpers
- Scan task creation

Moving these over is the next step. It needs a ClickUp equivalent of placement and assignment, so treat it as a separate piece of work.

## Testing

`tests/test_clickup_client.py` runs against a mocked HTTP transport, so it needs no token. It checks request shapes (URL, auth header, body, query), pagination, retries and error envelopes. **The client has not yet been run against a live ClickUp workspace.** Before relying on it, run it once with a real token against a test list.
