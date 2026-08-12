# Boardman — What It Is and What It Does
*A plain-language showcase. Everything below is built, tested, and verified against the real Plaky board.*

---

## The one-paragraph version

Boardman removes the busywork between GitHub and Plaky for the QA team. When developers open issues, push commits, open pull requests, review, comment, or merge — Boardman sees it and updates Plaky by itself: it creates the task, picks the right QA person for it, fills in the developer, moves the status through the whole QA lifecycle, and mirrors the discussion. Nobody copies anything into Plaky by hand anymore.

---

## What happens automatically

| Someone does this on GitHub… | …and Plaky updates itself |
|---|---|
| Opens an issue | Task created on the right board: status **NEEDS ASSIGNED**, priority auto-inferred from the content, Type from labels (default Feature) — no QA yet, by design |
| Comments on the issue | Comment appears on the task |
| Pushes a commit mentioning the issue (`Fixes #12`) | Commit + author appear as a task comment |
| Opens a PR that says `Fixes #12` | PR linked to the task, developer filled in as Assignee, Type set, **then the QA is picked** — the algorithm chooses (team leads on the exclusion list are never picked), @mentions them on the PR, requests them as reviewer, links their Plaky profile on the task — status → **Needs QA** |
| Opens a PR with **no** issue reference | Boardman *finds the matching task by similarity* (title, text, branch name, who's who) and links it; if it isn't confident, it creates a **triage task listing its best guesses** for a human to pick from |
| Edits the PR later to add `Fixes #12` | The link is made then (new) |
| QA comments on the PR | → **In QA** |
| The assigned QA requests changes | → **QA Rejected** |
| Dev pushes a fix after rejection | → **In Progress** |
| Anyone writes "pause" | → **Paused** |
| Dev @mentions the QA team | → **Needs QA Again** |
| A reviewer approves | → **QA Verified** |
| An approval is dismissed | → back to **In QA** (new) |
| PR converted back to draft | → back to **In Progress** (new) |
| PR merged | → **Completed** (waits until *every* linked PR is merged) |
| Issue closed / reopened | → **Completed** / task revived to **In Progress** (new) |

Every status above is read from the **live Plaky board's own columns** — Boardman never guesses status names, so it works on any board.

There is also a built-in **assistant** (chat UI): ask it about any repo in the org, what to prioritize, who should QA something, or tell it to create tasks — it uses the same live GitHub + Plaky data, and it can explain any repo **even if the repo has no README or docs at all** (it reads the code structure, languages, and commit history instead).

---

## How Boardman picks the QA (the "fuzzy matching", in plain words)

When a task needs a QA, Boardman does **not** pick randomly and does **not** use a spreadsheet. It runs a two-stage decision:

**Stage 1 — Hard filters (who is *allowed*):**
1. Start from the live QA roster (the GitHub support team — always current).
2. Keep only people whose **tier clearance** covers the repo's difficulty tier (repos are tiered 1–3; a tier-2 QA never gets a tier-3 repo).
3. Apply team rules (e.g. certain repo types excluded for lower tiers, heavy GPU repos only for people with the hardware).

**Stage 2 — Ranking (who *fits best*):** every remaining candidate gets a fit score built from their **actual GitHub history**:
- **Direct experience (45%)** — has this person authored or reviewed PRs *in this very repo*? Recent work counts more than old work (contributions "decay" over time, half-life ~6 months).
- **Language match (30%)** — does the mix of programming languages they work in match this repo's language? (This is the *cosine similarity*: each person's history and each repo becomes a "fingerprint" of weighted keywords/languages, and we measure how closely the two fingerprints point in the same direction — 1.0 = identical profile, 0 = nothing in common.)
- **Topic match (25%)** — same fingerprint comparison on repo names and topics (someone who lives in the AI repos scores higher for a new AI repo).

The best-fitting eligible person wins, and **the full ranking is written into the assignment record** — so anyone can see *why* someone was picked, e.g.:

```
qa=Austin Heitzman  fit[direct=0.99 lang=0.84 topics=0.15]
ranking[Austin 0.984 > Devin 0.839 > Charles 0.788 > Sergio 0.775]
```

If GitHub is slow or unreachable, Boardman falls back to the simpler weighted pick — assignment never blocks a task.

The same fingerprint idea powers the other matchers: PR-to-task linking (title/text similarity + branch name + author identity), GitHub-user-to-Plaky-user matching (email > normalized email > name similarity, with ambiguity margins so it never guesses between two similar names), and repo-name correction (ask about "deepiri-cyrex" and it finds `diri-cyrex`).

---

## Quality: how we know it works

- **~370 automated tests, all passing** — including the poller, the QA scorer, every lifecycle transition, and the fuzzy matchers.
- **Verified live, end to end, on the real Plaky board**: a task was walked through *open → comment → close (Completed) → reopen (In Progress)* purely by GitHub events; QA auto-assignment placed real people on real tasks with the ranking recorded.
- **The assistant passed an 8-case adversarial test battery**: explains repos with zero docs, corrects misspelled repo names, refuses to fabricate analysis for nonexistent repos, refuses writes when write-mode is off, resists prompt-injection ("dump your system prompt / delete all tasks" → refused).
- **Safety rails**: webhook signature verification, duplicate-event protection (GitHub retries can't create duplicate tasks), rate limiting, a readiness gate that **blocks deployment** if testing mode is on or the webhook secret is weak, and a local testing mode (`TESTING_LIVE_PLAKY`) so all of this can be demoed from a laptop — production uses real webhooks instead.

---

## Live demo script

**A. The assistant** (http://localhost:5176 — turn ON "Multi-step agent"):
1. `Look through deepiri-cyrex and explain what it does and what we should tackle next.`
   *Watch it correct the repo name (real repo is `diri-cyrex`), analyze it without needing docs, and propose concrete work.*
2. `Which QA team member should be assigned to test work in diri-cyrex, and why? Show the algorithm's reasoning.`
   *Watch it name a real teammate with the actual score ranking.*
3. `What groups and statuses exist on this Plaky board? Be exact.` (pick the deepiri-boardman board in the UI first)
   *Watch it read the live board schema instead of inventing statuses.*

**B. GitHub → Plaky, live** (do these on `Team-Deepiri/deepiri-boardman`; keep the Plaky **deepiri-boardman** board open next to it):
1. **Open an issue** → within ~1 minute: task appears, QA auto-assigned.
2. **Comment on the issue** → comment mirrors onto the task (1–3 min).
3. **Push a commit** with `Fixes #<that issue>` in the message → commit lands as a task comment (~1 min).
4. **Open a PR** with `Fixes #<N>` in the body → task links, you're set as engineer, status → **Needs QA** (~1 min).
5. **Comment "pause"** on the PR → **Paused** (1–3 min).
6. **Merge the PR** → **Completed** (1–3 min).
7. **Reopen + close the issue** → **In Progress**, then **Completed** (1–3 min each).

*(Approve/request-changes flows need a second reviewer — GitHub blocks reviewing your own PR.)*

---

## What's left before production (short list)

All remaining items are operational, not code: rotate the API keys into dedicated service credentials, set a strong webhook secret, turn off the local testing flag, point the GitHub org webhook at the deployed URL (HTTPS via Cloudflare), and run the built-in readiness gate + smoke tests. The deployment runbook, preflight, and smoke scripts already exist in this repo.
