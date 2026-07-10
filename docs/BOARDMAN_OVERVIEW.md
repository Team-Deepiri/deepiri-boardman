# Boardman — What It Is & Where We're At

_A quick read for the team._

---

## The problem

Right now, keeping Plaky in sync with what's happening on GitHub is manual. Someone has to find
the right task for a PR, assign the developer, pick a QA, and keep dragging the task through its
statuses as it gets reviewed, paused, reworked, and merged. It's tedious and it slips.

## What Boardman does

**Boardman is the robot that does all of that for you.** It listens to GitHub and updates Plaky
automatically — so a developer opens a PR and never has to touch the board.

When something happens on GitHub, Boardman:

1. **Finds the right Plaky task** for the PR.
2. **Fills in who's working on it** (the developer) and **assigns a QA** from the team.
3. **Moves the task's status** to match reality as the PR moves along.

No human in the loop. GitHub event in → Plaky updated out.

---

## The automation, in plain terms

**The status flow it drives automatically:**

```
Issue opened        →  Task created, QA auto-assigned
Issue commented     →  Comment mirrored onto the task
Issue closed        →  Completed
Issue reopened      →  In Progress (task revived)
PR opened           →  Assignee filled in, Type set, status → Needs QA
PR edited           →  Unlinked PR gets one more linking pass (late "Fixes #N")
PR back to draft    →  Needs QA reverted to In Progress
QA comments         →  In QA
QA requests changes →  QA Rejected
Dev pushes a fix    →  In Progress
Someone says "pause"→  Paused
Dev pings the QA    →  Needs QA (again)
QA approves         →  QA Verified
Approval dismissed  →  Back to In QA
PR merged           →  Completed
```

Placement precedence: an explicit `repos.yml` entry always wins; the Plaky-catalog
auto-discovery (repo-named group on the categorical boards) is the fallback for repos
nobody configured. PRs that can't be matched to any task create a **triage task listing
the pipeline's best guesses**, so a human can link in one click.

**How it picks the QA:** every repo has a difficulty **tier** (1–3), and every QA has a tier they're
cleared for. A tier-3 QA can review anything; a tier-1 QA only the simplest repos. Boardman looks at
who's eligible for that repo and assigns one. The QA list comes straight from the
`Team-Deepiri/support-team` GitHub team — no spreadsheet to maintain.

---

## How the matching works (the simple version)

The trickiest part is: _given a PR, which Plaky task is it about?_ Boardman uses three signals:

1. **Cosine similarity on the text.** Turn the PR's title into a bag of words, do the same for each
   Plaky task, and measure how much the two "point in the same direction." More shared, meaningful
   words → higher score → more likely the same work. (Think of it as: how much do these two
   sentences overlap, ignoring word order.)
2. **The branch name.** A branch like `feat/oauth-login` is a strong hint toward a task about OAuth
   login — and it tells us the **type** (feature/bug/etc.).
3. **The person.** It matches the PR author to a Plaky user by GitHub username, email, and name — so
   "this is Calista's PR" lines up with "Calista's task."

It blends those into one score. High score → link it automatically. Borderline → it can ask a small
AI model to break the tie. No match → it flags the PR for a human instead of guessing.

---

## Where we are

**The whole thing works, end to end.** We ran a full PR through it against a real Plaky board and
every step fired correctly — task created, QA assigned, developer filled in, and the status walked
all the way from _Needs Assigned_ to _Completed_.

✅ **Done**
- GitHub → Plaky sync (issues, PRs, reviews, comments), no duplicates
- Matching a PR to its task (text + branch + person)
- Auto-filling the developer and auto-assigning a tiered QA
- The full status state machine above
- 289 automated tests passing; full flow verified live

🔧 **What's left — setup, not building**
- Stand up the server and point GitHub's webhook at it
- Use a dedicated Plaky/GitHub service account (not a personal one) + generate secrets
- Sanity-check the auto-loaded QA roster (fix any wrong name→account matches)
- Flip each repo to its new board as the boards get organized

---

## Bottom line

The engine is built and proven. What remains is plugging it in: a server, a couple of service
accounts, and registering the webhook. After that, the board keeps itself up to date.

_Run it locally to watch it work: `poetry run python -m boardman.main`, then
`poetry run python scripts/sim_pr_lifecycle.py`. Go-live steps: `docs/GO_LIVE_CHECKLIST.md`._
