# deepiri-boardman — System Plan

## What This Is

`deepiri-boardman` is the automation and organization layer between GitHub and Plaky. It is a standalone Python service + installable CLI. It owns:

- The connection between GitHub repos and Plaky tasks (automated, no human in the loop)
- The "what does this repo need to do" intelligence (AI-driven task generation from repo direction)
- The organization of the Plaky board (routing tasks to the right table/group by repo type)

It does **not** touch Discord — that stays in norozo.

I implemented this already in deepiri-boardman. Okay, but I want to be able to, for instance, have a CLI that can detect what needs to be done. Like, I was describing creating issues on GitHub and linking them to Plaky, as well as an automated workflow service that exists in this repo that automates tasks, like sending a comment under a Plaky task that links to the GitHub PR. But how would we go about connecting them? Number one, that’s what I want, but I also want the ability, for instance, to have this be installable, use the deepiri-boardman command to point at a repo, and then it just creates Plaky tasks for that repo that need to be done stemming from the direction of the repo. Now, as I’m thinking, I’m not sure how to maintain and constantly update that direction of the repo so that the Plaky issues can be parsed from the direction—maybe it’s a Markdown file, maybe it’s a database, maybe I don’t know. But you see what I mean: I want to be able to point to a repo and then it just adds Plaky tasks to the board. Now, we do have the board currently hooked up with AI Table, ML/Data Table, Infrastructure Table, Backend/Database Table, and a Frontend Table, but it’s all mixed up for all the repos. Lowkey, I want to do minimal organization, and in fact, Deepiri Boardman should handle the organization. Make me a plan in deepiri-boardman/docs.
---

## What's Already Built (v0.1)

The core sync plumbing is done:

| Feature | How it works |
|---|---|
| GitHub issue opened → Plaky task | Webhook → `issue_handler.py` → `POST /tasks` |
| GitHub PR opened → Plaky comment | Webhook → `pr_handler.py` → `POST /tasks/{id}/comments` |
| GitHub PR merged → Plaky status update | Webhook → `pr_handler.py` → `PATCH /tasks/{id}` |
| CLI create task with repo tag | `boardman create-task --repo deepiri-sorge` |
| CLI sync open issues → Plaky | `boardman sync --repo owner/repo` |
| CLI list tasks | `boardman list` |
| GitHub ↔ Plaky mapping DB | `IssueTaskMap` in SQLite |

The connection is already working. Register a GitHub webhook at:
```
https://<host>:8090/api/v1/webhooks/github
Events: Issues + Pull requests
Secret: GITHUB_WEBHOOK_SECRET
```

---

## The Missing Piece: Repo Direction + AI Task Generation

### The Problem

Right now `boardman sync` pulls **existing open GitHub issues** and creates Plaky tasks from them. That's reactive — it only knows what's already been written down as an issue.

What we actually want: **point boardman at a repo and it figures out what needs to be done** — whether or not GitHub issues exist yet. Then it creates the Plaky tasks automatically.

### The Solution: `DIRECTION.md` + `boardman scan`

Each repo gets a `DIRECTION.md` file at its root. This is the single source of truth for where that repo is headed. It's human-written, version-controlled, and LLM-readable.

`boardman scan <repo>` reads the `DIRECTION.md`, looks at the repo's code structure, recent commits, and open issues, then uses an LLM (Claude API or local Ollama) to generate a list of specific, actionable Plaky tasks.

```
boardman scan owner/repo-name [--dry-run] [--model claude|ollama]
```

**What it does:**
1. Fetches `DIRECTION.md` from the repo root via GitHub API
2. Fetches recent commits (last 30 days) for context
3. Fetches open GitHub issues (already created tasks won't be duplicated — checked via `IssueTaskMap`)
4. Sends everything to the LLM with a structured prompt:
   - "Given the direction of this repo, what specific tasks are missing from Plaky?"
5. LLM returns a list of tasks with title, description, priority
6. Each task is created on Plaky with `[repo-name]` prefix + routed to the correct board table
7. Stored in `IssueTaskMap` so future syncs skip them (idempotent)

### The `DIRECTION.md` Format

Simple markdown. No special syntax required — the LLM reads it as prose.

```markdown
# deepiri-sorge Direction

## What This Repo Does
AI-powered GitHub PR review bot. Runs on GitHub Actions. Reads PR diffs and posts review comments using LangChain + Claude.

## Current Phase
v1 is deployed. We are now moving to v2 which adds multi-model support and a scoring rubric per PR size.

## What Needs to Be Done
- [ ] Add support for Gemini model alongside Claude
- [ ] Build a scoring rubric: small PR (<200 lines) = fast review, large PR = deep review
- [ ] Add a config file per repo (`.sorge.yml`) so teams can customize review depth
- [ ] Write integration tests against real GitHub webhook payloads
- [ ] Set up metrics endpoint (how many PRs reviewed, avg score)

## What's NOT In Scope
- UI dashboard (handled by deepiri-platform)
- Authentication (use GitHub App tokens only)
```

The `DIRECTION.md` can be updated any time. Running `boardman scan` again will only create tasks that don't already exist.

### Maintaining Direction Over Time

The direction doesn't update itself — a human (or an automated weekly review) writes/edits `DIRECTION.md`. Two options for keeping it fresh:

**Option A — Manual (start here):** Engineers update `DIRECTION.md` as part of their sprint planning or whenever the repo's goals shift. Low overhead.

**Option B — Automated weekly scan (future):** A cron job calls `boardman scan` on all registered repos every Monday. The boardman service posts a Discord message (via norozo) with a summary of what was generated. Engineers review and close/reassign as needed.

---

## Board Organization

### Current State (Messy)

The Plaky board has these tables mixing tasks from all repos:
- `AI Bugs / What to DO` (740 items)
- `ML What to DO / Bugs` (810 items)
- `Joe Black's Ideas` (40 items)
- `Main table` (everything else)

This doesn't scale. You can't tell what repo a task belongs to without reading it.

### The New Model: boardman owns the organization

boardman maintains a `repos.yml` file that maps each repo to a board table category:

```yaml
# deepiri-boardman/repos.yml
repos:
  deepiri-org/deepiri-sorge:
    category: ai
    plaky_table: AI Bugs / What to DO
    description: GitHub PR review bot

  deepiri-org/deepiri-norozo:
    category: infrastructure
    plaky_table: Infrastructure
    description: Discord bot

  deepiri-org/diri-cyrex:
    category: ai
    plaky_table: AI Bugs / What to DO
    description: AI agent / RAG service

  deepiri-org/deepiri-platform:
    category: backend
    plaky_table: Backend / Database
    description: Core platform API

  deepiri-org/deepiri-mudspeed:
    category: ml
    plaky_table: ML What to DO / Bugs
    description: GPU emulator with Neural ODE

  deepiri-org/deepiri-zepgpu:
    category: infrastructure
    plaky_table: Infrastructure
    description: Serverless GPU framework

  deepiri-org/deepiri-emotion-desktop:
    category: frontend
    plaky_table: Frontend
    description: Electron desktop IDE
```

When boardman creates a Plaky task, it reads `repos.yml` to route it to the right table. The `[repo-name]` prefix stays in the title so within each table you can still tell what repo it's from.

**Result:**
- Tasks are sorted by domain automatically
- You don't manually drag tasks to the right table
- Adding a new repo = one line in `repos.yml`
- Joe Black's Ideas table stays as-is for raw ideas before they become formal tasks

### Board Table Structure Going Forward

| Plaky Table | What goes here |
|---|---|
| `AI Bugs / What to DO` | cyrex, sorge, any LLM/agent repos |
| `ML What to DO / Bugs` | mudspeed, zepgpu, model research |
| `Infrastructure` | norozo, boardman, docker/k8s work |
| `Backend / Database` | platform core API, DB work |
| `Frontend` | emotion-desktop, platform UI |
| `Joe Black's Ideas` | raw ideas, not yet assigned to a repo |

---

## New CLI Commands to Build

On top of what already exists (`create-task`, `link-pr`, `list`, `sync`):

### `boardman scan`
```
boardman scan <owner/repo> [--dry-run] [--model claude|ollama]
```
AI-powered task generation from `DIRECTION.md`. Core new feature.

### `boardman init`
```
boardman init <owner/repo>
```
Creates a `DIRECTION.md` template in the repo via GitHub API (creates a file commit). Gets engineers to fill it in before running scan.

### `boardman register`
```
boardman register <owner/repo> --category ai|ml|backend|frontend|infrastructure
```
Adds a repo to `repos.yml` so boardman knows where to route its tasks.

### `boardman status`
```
boardman status [<owner/repo>]
```
Shows: how many tasks are in Plaky for this repo, what's open vs done, last scan date.

---

## How GitHub ↔ Plaky Connection Works (Full Picture)

```
GitHub Event (webhook) ──► boardman :8090 ──► Plaky API
      │                           │
      │                     IssueTaskMap DB
      │                     (SQLite)
      │
  PR opened / merged
      │
      ▼
  Find linked issue (#N via "Closes #N" in PR body)
      │
      ▼
  Look up plaky_task_id from IssueTaskMap
      │
      ▼
  POST /tasks/{id}/comments   (PR opened)
  PATCH /tasks/{id}           (PR merged → set status)
```

**The link between a PR and a Plaky task depends on:**
- The GitHub issue existing AND being in `IssueTaskMap`
- The PR body containing `Closes #N` or `Fixes #N`

**Workflow for contributors:**
1. A GitHub issue is created → boardman auto-creates the Plaky task
2. Developer creates a PR with `Closes #42` in the description
3. boardman sees the PR → adds a comment to the Plaky task with the PR link
4. PR merges → boardman sets Plaky task to `in_review` (or `done`)

No manual steps required once webhooks are registered.

---

## LLM Integration (for `boardman scan`)

### Two modes

**Mode 1 — Claude API (default, cloud)**
Uses the Anthropic API. Requires `ANTHROPIC_API_KEY` in `.env`.

```python
import anthropic

client = anthropic.Anthropic()
response = client.messages.create(
    model="claude-opus-4-6",
    max_tokens=2048,
    messages=[{"role": "user", "content": prompt}]
)
```

**Mode 2 — Ollama (local, offline)**
Uses the existing Ollama setup from the deepiri-platform. Requires Ollama running locally.

```python
import httpx
response = await client.post("http://localhost:11434/api/generate", json={
    "model": "llama3",
    "prompt": prompt,
    "stream": False
})
```

### Prompt structure for `boardman scan`

```
You are a software project manager. You are given the direction file for a GitHub repo.

REPO: {repo_name}
CATEGORY: {category}

DIRECTION FILE:
{direction_md_content}

RECENT COMMITS (last 30 days):
{commit_log}

CURRENTLY OPEN GITHUB ISSUES:
{issue_list}

EXISTING PLAKY TASKS FOR THIS REPO:
{plaky_task_list}

Generate a list of 3-8 specific, actionable tasks that are:
1. Aligned with the direction
2. Not already covered by open issues or existing Plaky tasks
3. Concrete enough for a developer to start immediately

Return JSON:
[
  {"title": "...", "description": "...", "priority": "low|medium|high"},
  ...
]
```

---

## Implementation Phases

### Phase 1 — Organization layer (build next)
- Add `repos.yml` to the repo root
- Update `issue_handler.py` and `pr_handler.py` to read `repos.yml` and route to the correct Plaky table (requires adding `table` param to Plaky API calls if supported, otherwise add as a tag/description note)
- Add `boardman register` CLI command

### Phase 2 — Direction + scan
- Add `boardman init` CLI command (creates `DIRECTION.md` template via GitHub API)
- Add `boardman scan` CLI command
- LLM integration (`boardman/llm/client.py` — wraps Claude API + Ollama fallback)
- New DB model: `ScanRun` (tracks when a repo was last scanned, what was generated)
- New `.env` variables: `ANTHROPIC_API_KEY`, `OLLAMA_BASE_URL`

### Phase 3 — Automated scan (optional / future)
- Cron in the FastAPI service: weekly `boardman scan` on all registered repos
- POST summary to norozo's internal API so it can alert Discord
- `boardman status` CLI command

---

## New Files to Create

```
deepiri-boardman/
├── repos.yml                          # Repo → board table routing
├── boardman/
│   ├── llm/
│   │   ├── __init__.py
│   │   └── client.py                  # Claude + Ollama wrapper
│   ├── services/
│   │   └── scan_handler.py            # Core logic for boardman scan
│   └── cli/
│       └── commands.py                # Add: scan, init, register, status
├── boardman/database/
│   └── models.py                      # Add: ScanRun model
└── docs/
    ├── PLAN.md                        # This file
    └── DIRECTION_TEMPLATE.md          # Template to copy into repos
```

---

## `.env` additions for Phase 2

```dotenv
# LLM (for boardman scan)
ANTHROPIC_API_KEY=sk-ant-...           # Claude API
OLLAMA_BASE_URL=http://localhost:11434 # Local Ollama fallback
DEFAULT_SCAN_MODEL=claude              # claude | ollama
```

---

## Open Questions / Decisions

1. **Does the Plaky API support routing to specific board tables/groups?** If yes, the `repos.yml` routing is clean. If not, we add the table name as a note in the task description as a fallback.

2. **Who runs `boardman scan` manually vs automated?** Start manual (Phase 1-2), automate later once the output quality is validated.

3. **Should `DIRECTION.md` live in each repo or in `deepiri-boardman/repos.yml`?** Recommendation: `DIRECTION.md` lives in each repo (version-controlled with the code that it describes), `repos.yml` in boardman contains only routing metadata.
