# Boardman: What It Is, In Plain Terms

A guide for anyone new to the project. No jargon.

---

## The one-sentence version

Boardman watches GitHub and keeps the Plaky board updated by itself, so nobody has to manually
move tasks around while a PR gets built, reviewed, and merged.

---

## The problem it solves

Today, keeping Plaky in sync with GitHub is a manual chore. A person has to find the right task for
a pull request, assign the developer, pick a QA, and keep dragging that task through its statuses as
the work is reviewed, paused, reworked, and merged. It is tedious and things slip through.

Boardman does all of that automatically. A developer opens a PR and never has to touch the board.

---

## A real example: follow one pull request

Here is exactly what happens, step by step, as a developer named Sam works on a feature.

1. Sam opens a GitHub issue: "Add OAuth login."
   → Boardman creates a Plaky task for it and assigns a QA from the team.

2. Sam opens a pull request from a branch named `feat/oauth-login`, writing "Fixes #42" in it.
   → Boardman finds the matching task, fills Sam in as the assignee, sets the task Type to "Feature"
     (it read that from the `feat/` branch name), and moves the task to Needs QA.

3. The assigned QA leaves a comment on the PR.
   → Boardman moves the task to In QA.

4. The QA requests changes.
   → Boardman moves the task to QA Rejected.

5. Sam pushes a new commit to fix it.
   → Boardman moves the task back to In Progress.

6. Sam comments "pausing this for now."
   → Boardman moves the task to Paused.

7. Sam comments "@QA ready for another look."
   → Boardman moves the task to Needs QA again.

8. The QA approves the PR.
   → Boardman moves the task to QA Verified.

9. The PR is merged.
   → Boardman moves the task to Completed.

Nobody touched the Plaky board. It stayed accurate the whole time.

---

## The full list of triggers

| What happens on GitHub | What Boardman does on Plaky |
| --- | --- |
| Issue opened | Creates the task, auto-assigns a QA |
| PR opened | Fills in the developer, sets the Type, moves to Needs QA |
| Assigned QA comments | Moves to In QA |
| QA requests changes | Moves to QA Rejected |
| Developer pushes a commit | Moves to In Progress |
| Someone comments "pause" | Moves to Paused |
| Developer @-mentions the QA | Moves to Needs QA again |
| QA approves | Moves to QA Verified |
| PR merged | Moves to Completed |

---

## How it picks the QA

Every repo has a difficulty tier from 1 to 3. Every QA is cleared up to a tier. A tier-3 QA can
review anything; a tier-1 QA only the simplest repos. Boardman picks an eligible QA automatically.

The QA list is not a spreadsheet we maintain. It comes straight from the `Team-Deepiri/support-team`
GitHub team, and Boardman matches each person to their Plaky account by name and email.

---

## How it figures out which task a PR belongs to

This is the only "smart" part. When a PR comes in, Boardman scores every task using three clues:

1. The words. It compares the PR title to each task title and measures how much they overlap,
   ignoring word order. This is the "cosine similarity" idea: two sentences that share a lot of the
   same meaningful words score high. "Add OAuth login" matches a task called "OAuth login support."
2. The branch name. A branch like `feat/oauth-login` points strongly at an OAuth task, and the
   `feat/` part also tells Boardman the task Type.
3. The person. It matches the PR author to a Plaky user by username, email, and name.

It blends these into one score. A high score links automatically. A borderline score can ask a small
AI model to break the tie. No good match means Boardman flags it for a human instead of guessing.

---

## How it will work in production, from a QA's point of view

For a QA, almost nothing changes in how they work, and that is the point.

- You keep doing reviews on GitHub like normal: comment, request changes, approve.
- The Plaky board updates itself to reflect what you did. You do not move cards.
- When a task is assigned to you as QA, that is Boardman matching you from the support team.
- The board is always trustworthy because a robot, not a person remembering to update it, keeps it
  current.

In short: QAs review on GitHub, and the board takes care of itself.

---

## How to test it (plain steps)

There are two ways.

Way 1: No GitHub needed (fastest). On a dev machine, start Boardman and run the simulator. It pretends
to be GitHub, sends a full PR's worth of events, and prints the task's status after each step.

```
poetry run python -m boardman.main
poetry run python scripts/edge_cases_live.py
```

You should see every status transition pass, plus the safety checks (duplicate events ignored, bad
signatures rejected, draft PRs handled).

Way 2: A real GitHub PR (the true test). Start Boardman, open a temporary public tunnel to it
(cloudflared or ngrok), add a webhook on one test repo pointing at that tunnel, then open a real issue
and PR and watch the board move. Full walkthrough is in `docs/PILOT_TESTING.md`.

Note: the QA steps only fire for the exact GitHub user Boardman assigned as QA. Check the task's "QA
Engineer Assigned" field to see who that is.

---

## What is blocking us from going to production

The engine is built, tested, and proven. What remains is plugging it in. Three real blockers:

1. A home for the backend. Boardman needs to run somewhere always-on (a server or container), not on
   someone's laptop. This decision is not finalized yet.
2. A public address. GitHub needs a public URL to send events to. That means a subdomain in front of
   the backend. Until that exists, we use a temporary tunnel for pilot testing.
3. Production keys. Boardman needs Deepiri's own service accounts for Plaky and GitHub, plus a webhook
   secret. These are supplied by Deepiri, not personal keys.

Everything else (the code, the boards, the QA roster) is ready.

---

## How we get to production, step by step

Phase 1 — Decide (unblocks everything)
- Pick where the backend runs (server or container platform).
- Provision Deepiri's Plaky service account and GitHub token.
- Confirm we are using the 5 new boards.

Phase 2 — Finish the code merge
- Merge the auto-discovery work (so the board/group is found automatically, no hardcoded IDs) with
  the QA-automation work into one branch.
- Land the lint cleanup PR.
- Confirm all tests pass on the combined branch.

Phase 3 — Pilot on one repo
- Stand up the backend, set the production keys and webhook secret.
- Add a webhook on one test repo (via tunnel if the subdomain is not ready).
- Run a real PR through it and confirm the board moves correctly.

Phase 4 — Roll out
- Point a real subdomain at the backend and switch to one org-level webhook for all repos.
- Spot-check the auto-assigned QA roster.
- Turn it on for repos in waves.

Phase 5 — Harden
- Rotate to final keys.
- Add an alert for any failed webhook.
- Add automatic retries if a Plaky update fails (a known small gap today).

---

## Team Q&A

Q: Are we still using the IDs from repos.yml to find the groups and boards?
A: Right now, yes. The running code looks up each repo's Plaky board and group ID from `repos.yml`.
The plan, which Joe asked for, is to stop doing that and instead auto-discover the board and group by
matching the repo name to the Plaky group. When that lands, `repos.yml` becomes empty and unused.
That work exists on a branch and needs to be merged in.

Q: For the backend, are we still using localhost for local/dev testing?
A: Yes. For development everything runs locally: the backend on localhost:8090 and the UI on
localhost:5176. That is dev only.

Q: Any migration or change planned for the backend worker when pushing to production?
A: The same worker runs in production, but as an always-on Docker container instead of on localhost,
in worker-only mode (no chat UI), using Deepiri's service-account keys, with its small SQLite database
saved to a persistent volume. There is no database engine migration planned for the first launch; it
stays SQLite. The real change is just where it runs and that it has a public address.

Q: Are we using a public API router or anything like that?
A: In production, yes, we need a public entry point so GitHub can deliver events. The plan is a
subdomain pointing at the backend, with a single org-level webhook. For pilot testing before that
subdomain is ready, we use a temporary tunnel. The exact hosting and routing setup is the open
decision to confirm with Joe and Ali.

Q: Can we make sure the lint-passing PR still follows the original functionality?
A: Yes, and there is a concrete way to check. After the lint PR, run the full test suite (288 tests)
and the live edge-case script against the test board. If both pass, behavior is unchanged. That is the
sign-off for "lint cleanup did not break anything."
