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

## Agent tools (Phase 1)

With `TASK_PROVIDER=clickup` the chat agent gets `clickup_*` tools instead of `plaky_*`:
`clickup_list_lists`, `clickup_list_tasks`, `clickup_get_task`, `clickup_list_workspace_users`, and (write mode) `clickup_create_task`, `clickup_create_tasks`, `clickup_update_task`, `clickup_add_comment`, `clickup_link_prs`, `clickup_create_subtask`.

- Assignees are plain names or emails, resolved against workspace members. Ambiguous or unknown names are reported back and left empty, never guessed.
- `clickup_create_tasks` skips titles already in the list and reports them as "Already in ClickUp".
- The system prompt gets a ClickUp notice that maps each `plaky_*` tool to its ClickUp equivalent. ClickUp has no board schema, groups or custom-field patching, so those tools have no counterpart.
- The agent's "board id" is a ClickUp list id (or `CLICKUP_DEFAULT_LIST_ID`).

## QA assignment (Phase 2)

QA picking is provider-neutral: `pick_qa_for_repo` ranks the GitHub support-team roster and returns a person id. What changed for ClickUp:

- **Blocking client:** loading the team roster is synchronous, so it uses a separate `SyncClickUpClient` (`boardman/clickup/sync.py`). `ClickUpClient` itself is purely async.
- **Roster ids:** when `TASK_PROVIDER=clickup`, `team_assignments` matches GitHub members to ClickUp workspace members (by name and email) and uses the ClickUp user id. A `member_overrides[login].id` still wins.
- **Applying QA:** if `CLICKUP_QA_FIELD_ID` is set (a custom field of type "users"), QA is written to that field. Otherwise the QA person is added as an extra assignee.
- **One entry point:** `update_task_internal` dispatches to `boardman/services/clickup_mutations.py` on ClickUp, so `PATCH /tasks/{id}`, the CLI and the agent all take the same `UpdateTaskInput` (status, priority, title, description, `qa_plaky_id`, or `auto_assign_qa` with `github_repo`). `task_type` has no ClickUp equivalent and is skipped.
- **Agent:** `clickup_update_task` takes `qa` (a plain name) or `auto_assign_qa` with `github_repo`.
- **Not supported yet:** engineer (developer) assignment is refused on ClickUp, because it needs the developer-eligibility rules that come with the webhook sync (Phase 3).

## Limits and gaps worth knowing

- **Plaky-only arguments are ignored.** `create_task` and `create_subtask` accept Plaky keyword arguments such as `field_values`, `person_field_keys`, `defer_field_patch` and `group_id` and ignore them, so shared call sites keep working. ClickUp has no equivalent of those fields.
- **Workspace choice.** If `CLICKUP_TEAM_ID` is not set, the first workspace the token can see is used and a warning is logged when there is more than one. Set `CLICKUP_TEAM_ID` to be explicit.

- `get_tasks` loads at most `CLICKUP_MAX_LIST_PAGES` pages of 100 tasks (default 20, so 2,000). If a list is larger, the result carries `truncated: true` and a message, and a warning is logged. Filter by status for very large lists.
- ClickUp has no field for a person's GitHub login, so `github_login` is always `None` on ClickUp users. GitHub-to-ClickUp matching uses name and email only (see `boardman/assignment/identity_match.py`), and a manual `member_overrides[login].id` always wins.

## Issue sync (Phase 3a)

With `TASK_PROVIDER=clickup`, GitHub issue webhooks (and the reconcile/poller replays that reuse the same handlers) create and maintain ClickUp tasks. `issue_handler` dispatches to `boardman/services/clickup_issue_sync.py`.

- **Where tasks go:** `clickup_list_id` on the repo's entry in `repos.yml` (or under `defaults`), else `CLICKUP_DEFAULT_LIST_ID`. With neither, creation fails with a clear message and nothing is mapped.
- **Created with everything set in one call:** title (`[repo] title`), description (issue body, URL, repo, category), priority, status, owner and tags (the repo name and `type:<bug|feature|...>`). No QA at creation; QA is picked when a PR opens.
- **Status follows ownership:** an owner who resolves to a real, developer-eligible ClickUp member means "assigned"; nobody means "needs assigned". Status names come from `CLICKUP_STATUS_*` (see below); an empty setting means Boardman never writes that status.
- **Edits rename in place.** ClickUp can rewrite a task's title and description, so an edit does (Plaky cannot and mirrors a comment instead).
- **Same safety rules as the Plaky path:** only `assigned`/`unassigned` events may move the status, never backwards past work that has started; the owner is fill-only on events that merely carry the issue's assignee; priority follows GitHub only when a human set it there; if the task cannot be read, none of an ownership event is applied.
- **Unassign** removes only the person who was removed, so a QA reviewer who is also an assignee stays.
- **Close and reopen:** closing sets the completed status, comments once and remembers the status the task held; reopening an owned issue resumes it (falling back to "assigned" if that status no longer exists), and an unowned one always goes to "needs assigned".
- **Type:** exactly one `type:` tag is kept in step with the issue's labels or native type.

Status settings (defaults suit a stock list; ClickUp statuses are per list, so match yours):

| Setting | Default |
|---|---|
| `CLICKUP_STATUS_NEEDS_ASSIGNED` | `to do` |
| `CLICKUP_STATUS_ASSIGNED` | `to do` |
| `CLICKUP_STATUS_IN_PROGRESS` | `in progress` |
| `CLICKUP_STATUS_PAUSED`, `_NEEDS_QA`, `_IN_QA`, `_APPROVED` | empty (not written) |
| `CLICKUP_STATUS_COMPLETED` | `complete` |

If "needs assigned" and "assigned" share a name (the default), the board cannot tell them apart. Give them different names in your list if you want the distinction.

## Pull request sync (Phase 3b)

`pr_handler` dispatches every PR event to `boardman/services/clickup_pr_sync.py` when `TASK_PROVIDER=clickup`: opened/reopened, edited, labeled/unlabeled, draft and ready-for-review, review requested/removed, pushes, closed, merged, inline review comments and deployment status. The reconcile sweep re-links PRs through the same code.

A PR attaches to the task its issue already owns (a closing keyword, a title reference, or an `issue-N` branch). It then:

- posts a "PR Opened/Reopened" notice once, keeps one `type:` tag in step with the PR's branch and labels, and fills the developer (an eligible developer who is the PR author) when nobody owns the task;
- assigns QA when the PR opens, never overwriting one: the QA users field if `CLICKUP_QA_FIELD_ID` is set, otherwise an extra assignee. QA is never the PR's author, bug-typed tasks go to the QA bug specialist, and the GitHub side is mentioned and requested as reviewer;
- asks for QA (`CLICKUP_STATUS_NEEDS_QA`) last, except for drafts.

The guards the Plaky path has, carried over:

- a late link, a replay or a reopen never moves a task backwards and never stages review work for a finished task;
- an edit never replaces a manual owner, and only a draft writes "assigned", and never over work that has started;
- a withdrawn review request re-queues a task but never over a QA verdict or a finished task;
- a push after a QA verdict means "in progress" (1 to 5 commits) or "needs QA again" (more), and only from a reviewed or in-progress status;
- closing without merging sends a task parked in the review queue back to "in progress" when no other PR is open;
- merging completes a task only for a closing keyword in the description, only when no other PR for it is open, and only once (a person's later move survives the reconcile sweep). A failed write is retried, not remembered as done;
- the assigned QA commenting means "in QA"; anyone else's comment is only mirrored; bot and Boardman's own comments are ignored; an edited comment updates the record, not the state;
- a successful deployment moves the task to "deployed" when `CLICKUP_STATUS_DEPLOYED` is set.

Extra status settings: `CLICKUP_STATUS_CHANGES_REQUESTED` and `CLICKUP_STATUS_DEPLOYED` (empty by default, so never written).

A PR that names no issue with a ClickUp task goes to fuzzy matching and orphan triage (see below).

## Review and comment sync (Phase 3c)

`pr_review_handler` dispatches PR reviews, PR conversation comments and plain-issue comments to `boardman/services/clickup_review_sync.py`.

- **Approve:** any reviewer's approval sets `CLICKUP_STATUS_APPROVED`, unless required checks on the head commit are failing (an approval is a verdict on the code, not the build). The PR's commit count is recorded as the baseline the push handler compares against. A dismissed approval goes back to "in QA".
- **Request changes:** counts only from the task's assigned QA (the users field, else the QA recorded on the PR's link row), and only when the reviewer maps to a ClickUp user. Anyone else's is ignored with a clear reason.
- **Comment review / conversation comment:** means "in QA" only from the assigned QA or a support-team member who is **not the PR's author**. If the author cannot be read, the roster alone no longer authorizes it (fail closed).
- **Dev commenting after a verdict** means "revisions in progress", never on a merged PR or a task that is not at a QA verdict. **A dev pinging QA** means "needs QA again". **Anyone saying "pause"** sets `CLICKUP_STATUS_PAUSED` (skipped with a message when it is not set).
- **Text is mirrored** to every linked task once per wording; an edit updates the record, never the state. Bots and Boardman's own comments are ignored. A comment on a plain issue lands on that issue's task.

## Creating tasks, the CLI and scans (Phase 4)

- **`POST /tasks`, `POST /tasks/{id}/subtasks`, `boardman create-task` and `create-subtask`** go through `create_task_internal` / `create_subtask_internal`, which dispatch to `create_clickup_task` / `create_clickup_subtask` (`services/clickup_mutations.py`). The list is `plaky_board_id` (a ClickUp list id), else the request's placement context, else `CLICKUP_DEFAULT_LIST_ID`. The status follows ownership unless one is named (a named status is used as written). A developer must be eligible. QA is assigned only when named, or when `auto_assign_team` is on and a repo is known. Repo names and the type become tags. `field_values` and `plaky_group_id` have no ClickUp meaning and are ignored. A subtask lands in its parent's list.
- **CLI:** `list`, `link-pr`, `status`, `sync` and `doctor` use the active provider (`sync` takes a list id for `--board-id` and does not need `--group-id` on ClickUp; `doctor` checks `CLICKUP_API_TOKEN` and the workspace). `plaky-inventory` and `capability-report` read Plaky boards and exit with a clear message on ClickUp.
- **Scans** (`boardman scan`, `scan-all`, the queued scan job): proposed tasks are filed in the repo's ClickUp list (`clickup_list_id` in `repos.yml`, else `CLICKUP_DEFAULT_LIST_ID`), tagged with the repo name. With neither, nothing is created and the result carries a warning saying why.
- **Deferred batch creation** (the queued job the Plaky agent uses) runs the ClickUp batch tool, so duplicates are skipped there too.

## PRs that name no issue, and the cleanup sweep

When a PR has no issue with a ClickUp task, it goes through the same fuzzy pipeline the Plaky path uses (`run_pr_task_pipeline_clickup`; the scoring and decisions are shared in `_decide`, only the candidates differ).

- **Candidates** are the tasks already owned by an issue of this repo plus the tasks in the repo's list. If the list is the repo's own (`clickup_list_id` in `repos.yml`), every task in it counts; a shared default list only contributes tasks that name the repo or an issue number. Finished work is scored down and live work up, using the `CLICKUP_STATUS_*` names.
- **A confident match** (`auto_link`, or an `llm_link` when `PR_LINKING_LLM_ENABLED`) links the PR, posts the notice, fills the developer, assigns QA and asks for QA, with the usual no-backwards guard on a replay.
- **No confident match:** when `ambiguous_pr.enabled` in `team_assignments.yml`, the PR gets a real task: titled after the PR, typed from its branch and labels, owned by the author, "needs QA" when it is not a draft, linked, and given a QA. It goes in `CLICKUP_TRIAGE_LIST_ID`, else the repo's list, else `CLICKUP_DEFAULT_LIST_ID`. It is created once per PR (even after the cleanup sweep removed the card), never for a PR that has already closed or merged, and a written issue reference with no task is claimed for the new card.
- **Cleanup sweep** (`boardman-worker`): an orphan task still sitting past `PR_TASK_CLEANUP_TTL_DAYS` is deleted in ClickUp and its audit row kept. Matched tasks whose PR merged and which reached the completed status are **archived in place** when `CLICKUP_ARCHIVE_COMPLETED_PRS=true`. ClickUp has no board-to-board move, so unlike Plaky nothing is recreated or deleted.

Not ported: Plaky's priority-precedent lookup for orphan tasks (the priority comes from the PR's own labels and text).

## Meeting plans (planning and huddle)

With `TASK_PROVIDER=clickup`, `ContextAggregator` builds the task section of a meeting plan from ClickUp (`planning/huddle/context_clickup.py`) instead of Plaky. The team-to-board mapping file (`PLANNING_TEAM_PLAKY_BOARDS_FILE`, default `team_plaky_boards.json`) is reused: the `board_id` values in it are ClickUp **list ids**. Items updated within `PLANNING_PLAKY_LOOKBACK_DAYS` are grouped by status (statuses in `PLANNING_PLAKY_HIGHLIGHT_STATUSES` first) with their assignees, and the section reads "ClickUp List Items". The "Boardman sync" section's headings (`PR <-> ClickUp task links`, `Issue <-> ClickUp mappings`) follow the provider too. The review-nudge sweep never read the board (it works from GitHub activity only), so it needed no change.

## Discovery routes and the inventory command

- **`GET /plaky/users`, `/plaky/boards`, `/plaky/boards/match`** use the active provider, so the UI's assignee and placement pickers work unchanged: on ClickUp "boards" are lists and users are workspace members, in the same response shape.
- **`/plaky/boards/{id}/groups` and `/groups/match`** return an empty list with an explanatory message on ClickUp (lists have no groups).
- **`/plaky/boards/{id}/schema`** returns the list's own statuses as a single Status field plus a markdown summary.
- **`boardman clickup-inventory [--list-id ID]`** prints the workspace members and lists, and with `--list-id` lists that list's statuses and checks every configured `CLICKUP_STATUS_*` name against them (exit code 1 on a mismatch). It is the quickest way to confirm a new API token works and that the status names in your settings exist in the list Boardman writes to.

## What is still Plaky-only

Plaky has board schemas, custom fields and per-board placement that ClickUp does not model the same way. These still call `PlakyClient` directly and are not provider-neutral yet:

- `plaky-inventory`, `capability-report` (the QA capability board), and the Plaky board-schema helpers

Moving these over is the next step. It needs a ClickUp equivalent of placement and assignment, so treat it as a separate piece of work.

## Testing

`tests/test_clickup_client.py` runs against a mocked HTTP transport, so it needs no token. It checks request shapes (URL, auth header, body, query), pagination, retries and error envelopes. **The client has not yet been run against a live ClickUp workspace.** Before relying on it, run it once with a real token against a test list.
