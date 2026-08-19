# Plaky automation — what to do to see it work

A walkthrough for demonstrating the GitHub → Plaky automation live, against the real
`Team-Deepiri/deepiri-boardman` repo and the **deepiri-boardman** Plaky board (`269031`).

Nothing here needs a webhook. While a Boardman instance runs with `TESTING_LIVE_PLAKY=true`
it polls GitHub and replays events through the same handlers a production webhook would hit,
so the board updates the same way it will in production.

## 1. Open an issue

Any issue in the repo. Within about a minute a task appears in **Open PRs** on the board:

| Field | Value | Where it comes from |
|---|---|---|
| Status | `NEEDS ASSIGNED` | default for a new issue — nobody owns it yet |
| Type | `Bug` / `Story` / `Task` / `Research` | issue labels, defaulting to Feature |
| Priority | `VERY IMPORTANT` … `Low` | inferred from the wording ("crashes", "blocker", "typo") |
| Assignee | empty | filled in from the PR that implements it |
| QA Engineer | empty | **assigned at PR time, not at task creation** |

QA is deliberately not picked here. Assigning a QA to work that has not been written yet
puts a name on a task nobody is going to look at for days.

## 2. Comment on the issue

The comment is mirrored onto the Plaky task, so QA discussion lives in one place.
Re-running or restarting Boardman does not repost it — mirrored comments are recorded in
`SyncLog` and matched on the comment URL.

## 3. Push a commit that references the issue

Any commit whose message contains `Fixes #78` (or `#78`) is commented onto the linked task
with the author and a link to the commit.

Commits are polled on the repo's default branch **and on the head branch of every open PR**.
A commit on a feature branch with no PR open is not seen — open the PR first.

## 4. Open the PR

This is where QA assignment happens: Boardman scores the QA roster, picks the best fit,
`@`-mentions them on the GitHub PR, requests them as a reviewer, and writes them into the
**QA Engineer Assigned** field on the task. Assignee and Type are filled from the PR.

## 5. Move it through review

| What you do on GitHub | What the task does |
|---|---|
| Mark the PR ready for review | → `Needs QA` |
| Convert back to draft | → `In Progress` |
| A reviewer approves | → `QA Verified` |
| The assigned QA requests changes | → `QA Rejected` |
| Merge the PR | → `Completed` |
| Close the PR without merging | → back to `In Progress` |
| Close the issue | → `Completed` |

Only the QA assigned on the task can send it to `QA Rejected` — a drive-by "request changes"
from anyone else does not move it.

## Turning it off

Set `TESTING_LIVE_PLAKY=false` in production. The poller never starts and the registered
GitHub webhook delivers the same events instead.
