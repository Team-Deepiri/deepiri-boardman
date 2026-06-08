# deepiri-boardman — System Plan

**This file is the single planning source of truth:** GitHub ↔ Plaky automation, `DIRECTION.md` / `boardman scan`, board routing, **and** the AI Board Manager assistant (LangChain, memory, multi-provider LLM, Plaky power tools). For module-level agent detail, see [AGENT_PLAN.md](./AGENT_PLAN.md). For coding-agent context, see [AGENTS.md](../AGENTS.md).

**Doc maintenance:** Coding agents and contributors must keep this file aligned with the repo when features ship or interfaces change. See [AGENTS_MAINTENANCE.md](./AGENTS_MAINTENANCE.md) (PLAN.md triggers).

---

## What This Is

`deepiri-boardman` is the automation and organization layer between GitHub and Plaky. It is a standalone Python service + installable CLI. It owns:

- The connection between GitHub repos and Plaky tasks (automated, no human in the loop)
- The "what does this repo need to do" intelligence (AI-driven task generation from repo direction)
- The organization of the Plaky board (routing tasks to the right table/group by repo type)
- A conversational AI product manager (**shipped**): repo-grounded planning, task create/update in Plaky, session memory, tool guardrails (`allow_writes`), LangChain tools

It does **not** touch Discord — that stays in norozo.

---

## What's Built

### Core sync (v0.1)

| Feature | How it works |
|---|---|
| GitHub issue opened → Plaky task | Webhook → `issue_handler.py` → `POST /tasks` |
| GitHub PR opened → Plaky comment | Webhook → `pr_handler.py` → `POST /tasks/{id}/comments` |
| GitHub PR merged → Plaky status update | Webhook → `pr_handler.py` → `PATCH /tasks/{id}` |
| CLI create task with repo tag | `boardman create-task --github-repo owner/repo` |
| CLI sync open issues → Plaky | `boardman sync --repo owner/repo` |
| CLI list tasks | `boardman list` |
| GitHub ↔ Plaky mapping DB | `IssueTaskMap` in SQLite |

### Shipped since v0.1

| Feature | How it works |
|---|---|
| `repos.yml` board routing | `boardman register`; `plaky/placement.py` |
| `DIRECTION.md` + AI scan | `boardman scan`, `POST /api/v1/agent/scan`; `services/scan_handler.py` |
| Direction bootstrap | `boardman init`, `POST /api/v1/agent/init-direction` |
| LangChain agent + memory | `boardman agent chat/ask`, `POST /api/v1/agent/chat` (+ stream, async jobs) |
| Agent tools | `plaky_tools`, `repo_tools`, `github_tools`, `assignment_tools` |
| Web UI | `boardman-ui/` — Vite/React chat; nginx proxy `:8088` in Docker |
| QA assignment / tiering | `assignment/`, `github/qa_*`; optional Cloudflare `worker/` |
| PR↔task fuzzy linking | `services/pr_task_linking.py`, `PullRequestTaskLink` table |
| PR QA workflow | `pr_handler.py` — Needs QA / In QA when env statuses configured |
| Background jobs | SQLite `background_jobs` + `boardman-worker` |
| Readiness / doctor | `boardman readiness`, `boardman doctor` |

The connection is already working. Register a GitHub webhook at:

```
https://<host>:8090/api/v1/webhooks/github
Events: Issues + Pull requests
Secret: GITHUB_WEBHOOK_SECRET
```

---

## Repo Direction + AI Task Generation (shipped)

`boardman sync` is reactive — it only creates Plaky tasks from **existing open GitHub issues**.

`boardman scan` is proactive — it reads each repo's `DIRECTION.md`, recent commits, and open issues, then uses an LLM to propose tasks not already on the board.

### `DIRECTION.md` + `boardman scan`

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

## CLI commands

Full examples: [README.md](../README.md).

| Command | Status | Notes |
|---------|--------|-------|
| `create-task`, `update-task`, `create-subtask`, `link-pr`, `list`, `sync` | Shipped | Core Plaky + GitHub sync |
| `boardman scan <owner/repo> [--dry-run]` | Shipped | AI task generation from `DIRECTION.md` |
| `boardman scan-all [--dry-run]` | Shipped | Scan all registered repos |
| `boardman init <owner/repo>` | Shipped | Bootstrap `DIRECTION.md` via GitHub PR |
| `boardman register <owner/repo> --category …` | Shipped | Add repo to `repos.yml` |
| `boardman status [<owner/repo>]` | Shipped | Task counts, last scan date |
| `boardman agent chat/ask -m "…"` | Shipped | Agent chat (`--allow-writes`, `--use-tools`, `--session`) |
| `boardman doctor` | Shipped | Ollama + Plaky + config checks |
| `boardman readiness` | Shipped | Go-live readiness report |
| `boardman plaky-inventory` | Shipped | Board schema/users/groups export |

**Not built:** interactive agent REPL (`boardman agent --repo`), `boardman agent generate`, `boardman agent sessions`, `boardman config set` — use `agent chat` with `session_id` instead.

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

## LLM Integration

### `boardman scan` — two modes (reference)

**Mode 1 — Claude API (default, cloud)** — requires `ANTHROPIC_API_KEY` in `.env`.

```python
import anthropic

client = anthropic.Anthropic()
response = client.messages.create(
    model="claude-opus-4-6",
    max_tokens=2048,
    messages=[{"role": "user", "content": prompt}]
)
```

**Mode 2 — Ollama (local, offline)** — uses Ollama from deepiri-platform patterns or local Docker.

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

### Providers (scan + agent)

| Provider | Env | Notes |
|----------|-----|--------|
| Ollama | `OLLAMA_BASE_URL`, `LLM_MODEL` | Primary local; mirror `deepiri-platform/diri-cyrex/app/integrations/ollama_container.py` (URL detection, `/api/chat`, `/api/tags`). Optional `ollama` service in `docker-compose.yml`. |
| OpenAI | `OPENAI_API_KEY` | Strong tool-calling; LangChain adapter |
| Anthropic | `ANTHROPIC_API_KEY` | Same family as scan example above |
| Google Gemini | `GEMINI_API_KEY` (or ADC) | Verify model IDs periodically |

**Settings:** `LLM_PROVIDER`, `LLM_MODEL`, optional **fallback chain** (e.g. Ollama → cloud on timeout), logged.

### LangChain (agent runtime)

- **Dependencies (indicative):** `langchain`, `langchain-community`, `langchain-openai`, `langchain-anthropic`, `langchain-google-genai`; optional `langgraph` later.
- **Chat model:** One interface per provider; uniform tools + history.
- **Tools:** `@tool` functions, small arity, JSON-serializable returns, explicit docstrings.
- **Memory v1:** SQL-backed history keyed by `session_id`; optional summary buffer for long threads.
- **Structured output:** Pydantic / tool call for “plan as task list” before Plaky writes.

---

## AI Board Manager — full assistant master plan

**Goal:** A **repo-grounded, skeptical** AI product manager that converses, reads docs and (optionally) live repo state, co-designs a plan, and **creates / updates / reorganizes** Plaky work with **session memory**, **tool guardrails**, and **pluggable LLMs**.

### Is this “too much”?

A full assistant with memory and board surgery is a large surface. **Do not** ship Redis, vector DB, and heavy guardrail frameworks in v1.

- **Use LangChain** as the integration layer for prompts, tool calling, providers.
- **LangGraph** is optional until you need explicit state machines (`gathering_context` → `awaiting_user_choice` → `executing_plaky_writes`) with clean human-in-the-loop interrupts.

| Layer | v1 | v2+ |
|--------|----|-----|
| Orchestration | LangChain agent + tools | LangGraph + checkpoints |
| Memory | DB sessions + messages; optional summary | Vector / long-horizon summaries |
| Queue | In-process / FastAPI | Redis/RQ or Celery for deep scans |
| Guardrails | Prompt + tool policy + confirm destructive | Moderation, strict JSON validators |
| LLM | Ollama + one cloud fallback | Full factory + route by task type |

### “Deepiri AXIOM” behavior (not a code dependency)

**deepiri-axiom** installs rigorous prompts into dev tools. Boardman **inherits the contract**, not the package:

- **Doc-grounded:** Prefer `README`, `docs/`, `SPEC.md`, `DIRECTION.md`, ADRs, tool outputs over priors.
- **Skeptical:** Cross-check; say when data is missing or stale.
- **Current-aware:** Infer stack from lockfiles and repo files; do not rely on training cutoff.
- **Discerning:** Honor explicit “not in scope” in direction docs.

### Target architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        deepiri-boardman (FastAPI + CLI)                  │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────────────┐ │
│  │ CLI (Typer)  │  │ REST /api/v1 │  │ Agent (LangChain):              │ │
│  └──────┬───────┘  └──────┬───────┘  │  system + tools + memory        │ │
│         └────────┬────────┴──────────┴───────────────┬──────────────────┘ │
│  ┌───────────────▼───────────────┐  ┌───────────────▼──────────────────┐ │
│  │ LLM factory (Ollama/OpenAI/    │  │ Tools: repo | plaky | github |   │ │
│  │ Anthropic/Gemini)              │  │ memory (schema + guard policy)    │ │
│  └───────────────────────────────┘  └──────────────────────────────────┘ │
│  ┌───────────────▼───────────────┐  ┌───────────────▼──────────────────┐ │
│  │ SQLite (+ agent tables)        │  │ Plaky HTTP (extend client)        │ │
│  └───────────────────────────────┘  └──────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

### Package layout (current)

See [AGENT_PLAN.md](./AGENT_PLAN.md) for module detail. Implemented tree:

```
boardman/
├── llm/
│   ├── factory.py, completion.py, ollama_autodetect.py
├── agent/
│   ├── prompts.py, memory_store.py, guardrails.py, runner.py, service.py
│   └── tools/
│       ├── plaky_tools.py, repo_tools.py, github_tools.py, assignment_tools.py
```

### Memory, state, queue

- **Session:** `session_id`, optional `repo`, timestamps.
- **Messages:** append-only; optional `tool_calls` / `tool_results` JSON for audit.
- **Project context:** per-repo summary, goals, constraints — updated by tools and scan.
- **Tables:** `AgentSession`, `AgentMessage`, `ProjectContext` (Alembic) — see AGENT_PLAN.md.
- **Agent state v1:** Implicit in conversation + `pending_confirmation` in app code.
- **Queue v1 (shipped):** SQLite `background_jobs` + `boardman-worker` for async agent chat and Plaky reorder jobs.
- **Queue v2+:** Optional Redis/Kafka for multi-region scale (not required).

### Long system prompt (v1 — ship in `boardman/agent/prompts.py`)

Baseline; split into constants (`CORE_BEHAVIOR`, `PLAKY_RULES`, `TOOL_DISCIPLINE`) as needed.

```text
You are the Deepiri Board Manager — an AI product and delivery partner for software teams.

## Mission
Help the user understand a repository’s direction, surface gaps, co-design a plan, and translate that plan into actionable work in Plaky (tasks, subtasks, ordering, status) — without replacing human judgment.

## Epistemic stance (non-negotiable)
- Ground claims in evidence: repository files, tool outputs, and user messages. If you have not read it, do not pretend you have.
- Be skeptical of your own prior knowledge: libraries, APIs, and best practices change. Prefer what the repo’s docs and config files say.
- If evidence is missing, stale, or contradictory, say so and ask a targeted question or propose a small discovery step (read file X, list open issues).
- Never invent Plaky task IDs, URLs, or API fields. Use tools to fetch current data.

## Currentness and “what’s true now”
- When describing stack or versions, look for lockfiles, package manifests, CI configs, and README dates.
- Do not assert deprecation or security status unless you saw it in project materials or the user supplied it.
- When uncertain, use precise uncertainty: “Not verified in-repo” vs “Confirmed in README”.

## How you collaborate
1. Clarify intent: new feature, new repo bootstrap, “what’s next”, or “improve health” — one short pass of questions if needed.
2. Gather context: prefer DIRECTION.md / README / docs/ / SPEC.md; respect explicit “out of scope” sections.
3. Propose a plan: goals, milestones, risks, and a ordered backlog slice (not 50 tasks at once unless asked).
4. Confirm before destructive or broad changes: deleting tasks, mass moves, or rewrites of many descriptions.
5. Execute via tools: create/update/move tasks, add subtasks, reorder when supported.

## Plaky discipline
- Route work to the correct board area using team conventions (AI vs ML vs infra vs backend vs frontend vs ideas). If routing is ambiguous, ask once.
- Titles: clear, verb-led, unique enough to grep; include repo identifier when the team does that today.
- Descriptions: acceptance notes, links to docs, and dependencies — not vague slogans.
- Prefer updating existing tasks when the work already exists; avoid duplicates. Use list/search tools first.

## Tool use
- Call tools instead of hallucinating data.
- After tool errors, summarize the error for the user and suggest a fix (permissions, missing API field, wrong id).
- Batch read operations when possible; write operations should be deliberate and logged in your reply.

## Tone
Professional, concise, direct. No filler. Surface tradeoffs and risks early.
```

Bump **`PROMPT_VERSION`** when this changes; store on `AgentSession` for debugging.

### Guardrails

- **Tool allowlist** only; no arbitrary shell.
- **Risk classes:** reads always OK; writes / delete / mass move require **explicit user confirmation** (e.g. “yes, apply” after preview) or `CONFIRM_TOKEN` on API.
- **Rate cap** Plaky mutations per turn; log counts.
- **Data:** exclude `.env` from scanners; local-only deployments force `LLM_PROVIDER=ollama` if required by policy.

### Tools catalog

**Repo / docs**

| Tool | Purpose |
|------|---------|
| `scan_local_repo` | Path on disk: bounded tree, key docs, optional TODO/FIXME search |
| `fetch_repo_doc` | GitHub API: file at path@ref |
| `summarize_direction` | `DIRECTION.md` + open issues → structured summary |

**Plaky**

**Shipped** (`boardman/plaky/client.py`): `create_task`, `get_tasks`, `get_task`, `add_comment`, `update_task_fields`, `create_subtask`, `patch_item_field_values`, board schema/inventory helpers. Agent tools in `plaky_tools.py` wrap these.

**Partial / ongoing:** column move, bulk reorder, assignee updates — some paths exist via field patches; verify against live Plaky board schema. Archive/delete behind `allow_writes` + confirmation.

**GitHub (read-mostly v1):** list issues, file content, recent commits — avoid duplicating `issue_handler.py` logic.

**Memory tools (optional):** `save_project_note` / `get_project_notes` for preferences not stored in Plaky.

### User journeys

1. **New feature** → clarify → scan docs → plan → approve → tasks + subtasks in Plaky.
2. **What’s next** → direction + issues + Plaky → ranked suggestions → optional create.
3. **Improve repo** → docs/architecture → user picks → tasks.
4. **Organize board** → list state → preview moves/reorder → confirm → execute.
5. **Revise tasks** → edit, split subtasks, reassign (confirm if bulk).

### Agent API (shipped)

- `POST /api/v1/agent/chat` — `{ message, session_id?, repo?, allow_writes?, provider?, model?, queue? }`
- `POST /api/v1/agent/chat/stream` — streaming variant
- `GET /api/v1/agent/jobs/{job_id}` — async job status (when `queue=true`)
- `GET /api/v1/agent/sessions/{id}/history`
- `DELETE /api/v1/agent/sessions/{id}`
- `POST /api/v1/agent/scan` — LLM scan (`dry_run`, `repo`, …)
- `POST /api/v1/agent/init-direction` — bootstrap `DIRECTION.md` PR

**Not built:** `GET /api/v1/agent/sessions` (list all), direct tool endpoints (`scan-repo`, `create-tasks`, `organize-board`) — use chat with tools instead.

### Web UI (shipped)

**Package:** `boardman-ui/` — Vite + React chat shell derived from Cyrex (chat panel + MessagesWidget only; no spreadsheet/playground).

| Environment | URL | Notes |
|-------------|-----|-------|
| Local dev | `http://localhost:5176` | `npm run dev`; proxies `/api` → `:8090` |
| Docker prod | `http://localhost:8088` | `boardman-nginx` serves `dist/` + `/api` proxy |

See [boardman-ui/README.md](../boardman-ui/README.md) and [DEPLOYMENT.md](./DEPLOYMENT.md).

**Historical note:** v1 was copied from `deepiri-platform/diri-cyrex/cyrex-interface/` (chat tab + MessagesWidget only). Cyrex copy instructions are archived in git history if needed.

---

## Implementation phases (unified)

| Phase | Scope | Status |
|-------|-------|--------|
| **A** — Organization layer | `repos.yml`, routing in handlers, `boardman register` | **Done** |
| **B** — Direction + scan | `init`, `scan`, `llm/factory.py`, `ScanRun` table | **Done** |
| **C** — Automated scan | Cron weekly scan; norozo/Discord summary | **Not started** |
| **D** — Agent prerequisites | `PROMPT_VERSION`, Plaky API verification | **Done** |
| **E** — Agent skeleton | LangChain + tools, `boardman agent chat/ask` | **Done** |
| **F** — Agent memory | `AgentSession`, `AgentMessage`, `ProjectContext` | **Done** |
| **G** — Repo grounding | `repo_tools.scan_local_repo`, `DIRECTION.md` / README | **Done** |
| **H** — Plaky power tools | Subtasks, field updates, agent `plaky_tools` | **Mostly done** — bulk reorder/move still evolving |
| **I** — Polish | `boardman doctor`, SQLite job queue, rate limits | **Done** — LangGraph still v2+ |
| **J** — Web UI | `boardman-ui/`, nginx in Docker | **Done** |

Future work: see [NEW_FEATURES_PLAN.md](./NEW_FEATURES_PLAN.md) (bidirectional sync, etc.).

---

## Current package layout

```
deepiri-boardman/
├── AGENTS.md, CLAUDE.md         # Agent context (machine-oriented)
├── pyproject.toml, poetry.lock
├── repos.yml, team_assignments.yml
├── boardman-ui/                 # Vite + React agent chat (shipped)
├── boardman/
│   ├── main.py, settings.py
│   ├── cli/commands.py
│   ├── routes/                  # agent, tasks, github_events, plaky, repos, assignment, health
│   ├── services/                # issue/pr/scan handlers, PR linking, task mutations
│   ├── plaky/, github/, assignment/
│   ├── llm/                     # factory.py, completion.py, ollama_autodetect.py
│   ├── agent/                   # runner, service, prompts, guardrails, memory_store, tools/
│   ├── database/models.py
│   ├── broker/, jobs/, cache/, ratelimit/
│   └── sqlite_worker.py
├── alembic/versions/            # 001–004 migrations
├── tests/
├── scripts/                     # deploy_preflight, deploy_smoke, verify_agents_md
├── worker/                      # Optional Cloudflare QA proxy
└── docs/
    ├── PLAN.md                  # This file
    ├── AGENT_PLAN.md, AGENTS_MAINTENANCE.md
    ├── DEPLOYMENT.md, BOARDMAN_*.md
    ├── NEW_FEATURES_PLAN.md, ADDITIONAL_FEATURES.md
    └── DIRECTION_TEMPLATE.md
```

---

## Configuration (`.env`)

```dotenv
# LLM (scan + agent)
LLM_PROVIDER=ollama
LLM_MODEL=llama3:8b
OLLAMA_BASE_URL=http://localhost:11434
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GEMINI_API_KEY=

# Agent
AGENT_MAX_HISTORY=50
AGENT_REQUIRE_CONFIRM_BULK=true
PROMPT_VERSION=2026-04-09

# Plaky / GitHub (existing)
# PLAKY_API_KEY, GITHUB_WEBHOOK_SECRET, ...
```

---

## Open questions, risks, and decisions

1. **Plaky API:** Does it support routing to specific tables/groups? If not, encode table in description/tags until API supports it.

2. **Manual vs automated scan:** Start manual; automate after quality is trusted.

3. **`DIRECTION.md` location:** Per-repo (canonical) + optional mirror/cache in `ProjectContext` after scan — **both** recommended.

| Risk | Mitigation |
|------|------------|
| Plaky lacks reorder/subtasks | Fallback: checklists in description or comments |
| Weak local tool calling | Cloud for tool-heavy turns or small router model |
| Huge repos / token limits | Bounded tree; exclude `node_modules`, `.git`, binaries |
| Prompt drift | `PROMPT_VERSION` per session |
| Duplicate tasks | Preflight search before create |

---

## Verification checklist (agent + scan + UI)

- [x] Ollama works from container and host with documented ports (`docker-compose.yml`, README)
- [x] Provider switch is env-only + restart (`LLM_PROVIDER`, `LLM_MODEL` in settings)
- [x] Session survives restart (DB history — `AgentSession` / `AgentMessage`, tests)
- [x] Agent does not invent task IDs (guardrails + tool-first policy; `test_agent_guardrails.py`)
- [x] Bulk Plaky change requires confirmation when flag on (`AGENT_REQUIRE_CONFIRM_BULK`, `allow_writes`)
- [x] Scan/agent repo tools return real paths/excerpts (`repo_tools`, `test_tools.py`)
- [x] `boardman-ui` dev server proxies API to boardman; agent chat round-trips in browser

---

## Design notes (archive)

Original product intent (pre-implementation): installable `boardman` CLI that points at a repo, reads direction (settled on `DIRECTION.md`), creates Plaky tasks, and automates GitHub↔Plaky linking (PR comments, status). Boardman owns Plaky table organization via `repos.yml` so tasks are not mixed across AI/ML/infra/backend/frontend domains.

---

## Document history

| Date | Change |
|------|--------|
| 2026-04-09 | UI/nginx plan; Python deps via **Poetry** (`pyproject.toml` + `poetry.lock`); Docker uses Poetry. |
| 2026-06-05 | Reconciled shipped vs planned: agent, scan, UI, worker marked done; added AGENTS.md maintenance note; verification checklist checked off. |
