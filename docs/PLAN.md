# deepiri-boardman — System Plan

**This file is the single planning source of truth:** GitHub ↔ Plaky automation, `DIRECTION.md` / `boardman scan`, board routing, **and** the AI Board Manager assistant (LangChain, memory, multi-provider LLM, Plaky power tools). For module-level agent sketches, see [AGENT_PLAN.md](./AGENT_PLAN.md).

---

## What This Is

`deepiri-boardman` is the automation and organization layer between GitHub and Plaky. It is a standalone Python service + installable CLI. It owns:

- The connection between GitHub repos and Plaky tasks (automated, no human in the loop)
- The "what does this repo need to do" intelligence (AI-driven task generation from repo direction)
- The organization of the Plaky board (routing tasks to the right table/group by repo type)
- **(Planned)** A conversational AI product manager: repo-grounded planning, task create/update/reorganize in Plaky, session memory, tool guardrails

It does **not** touch Discord — that stays in norozo.

I implemented this already in deepiri-boardman. Okay, but I want to be able to, for instance, have a CLI that can detect what needs to be done. Like, I was describing creating issues on GitHub and linking them to Plaky, as well as an automated workflow service that exists in this repo that automates tasks, like sending a comment under a Plaky task that links to the GitHub PR. But how would we go about connecting them? Number one, that’s what I want, but I also want the ability, for instance, have this be installable, use the deepiri-boardman command to point at a repo, and then it just creates Plaky tasks for that repo that need to be done stemming from the direction of the repo. Now, as I’m thinking, I’m not sure how to maintain and constantly update that direction of the repo so that the Plaky issues can be parsed from the direction—maybe it’s a Markdown file, maybe it’s a database, maybe I don’t know. But you see what I mean: I want to be able to point to a repo and then it just adds Plaky tasks to the board. Now, we do have the board currently hooked up with AI Table, ML/Data Table, Infrastructure Table, Backend/Database Table, and a Frontend Table, but it’s all mixed up for all the repos. Lowkey, I want to do minimal organization, and in fact, Deepiri Boardman should handle the organization.

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

### Agent (planned)

```
boardman agent --repo <slug>                    # interactive REPL, sticky session
boardman agent chat --session ... --message "..."  # one-shot
boardman agent scan --path ...                  # repo context only, no Plaky writes
boardman doctor                                  # Ollama reachable, model pulled, Plaky token valid
```

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

### Package layout (target)

See [AGENT_PLAN.md](./AGENT_PLAN.md) for diagrams; target tree:

```
boardman/
├── llm/
│   ├── factory.py, ollama.py, openai.py, anthropic.py, gemini.py
├── agent/
│   ├── prompts.py, memory_store.py, guardrails.py, runner.py
│   └── tools/
│       ├── repo_context.py, plaky_tasks.py, plaky_board.py, github_read.py
```

### Memory, state, queue

- **Session:** `session_id`, optional `repo`, timestamps.
- **Messages:** append-only; optional `tool_calls` / `tool_results` JSON for audit.
- **Project context:** per-repo summary, goals, constraints — updated by tools and scan.
- **Tables:** `AgentSession`, `AgentMessage`, `ProjectContext` (Alembic) — see AGENT_PLAN.md.
- **Agent state v1:** Implicit in conversation + `pending_confirmation` in app code.
- **Queue v1:** None; **v2:** background jobs for heavy repo scans.

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

**Today** (`boardman/plaky/client.py`): `create_task`, `get_tasks`, `get_task`, `add_comment`, `update_task_status`.

**Needed** (verify live Plaky API): columns, move task, subtasks, reorder within column, title/body/assignee/priority updates, archive/delete (behind confirmation). Each: `PlakyClient` method + LangChain tool.

**GitHub (read-mostly v1):** list issues, file content, recent commits — avoid duplicating `issue_handler.py` logic.

**Memory tools (optional):** `save_project_note` / `get_project_notes` for preferences not stored in Plaky.

### User journeys

1. **New feature** → clarify → scan docs → plan → approve → tasks + subtasks in Plaky.
2. **What’s next** → direction + issues + Plaky → ranked suggestions → optional create.
3. **Improve repo** → docs/architecture → user picks → tasks.
4. **Organize board** → list state → preview moves/reorder → confirm → execute.
5. **Revise tasks** → edit, split subtasks, reassign (confirm if bulk).

### Agent API (planned)

- `POST /api/v1/agent/chat` — `{ message, session_id?, repo? }` → `{ reply, session_id, ... }`
- `GET /api/v1/agent/sessions/{id}/history`
- `DELETE /api/v1/agent/sessions/{id}`

### Web UI — reuse Cyrex interface (Vite + React)

**Source tree (monorepo):** `deepiri-platform/diri-cyrex/cyrex-interface/`

**v1 scope (intentionally narrow):** only **(1) MessagesWidget** and **(2) the main Cyrex “Interactive Chat” panel** — transcript bubbles, composer, Send, connection header, and the same dark monospace chat chrome. **No** spreadsheet, **no** Agent Playground, **no** LiveSpreadsheet, **no** orchestration/workflow/RAG/debug tabs from Cyrex.

Board Manager gets a **separate** package (e.g. `deepiri-boardman/boardman-ui/`): **Vite 5 + React 18 + TypeScript**, minimal shell (sidebar can be a single “Chat” view + control to open MessagesWidget). Do **not** copy all of `App.tsx`; copy Tier 1 shell + **extract** the chat tab into a small `BoardmanChatPanel.tsx` (or inline in `App.tsx` if tiny). Proxy `/api` → boardman **8090** in `vite.config.ts`.

#### Tier 1 — copy (then rename / tweak labels)

| Source file | Purpose |
|-------------|---------|
| `package.json` | Scripts (`dev`, `build`, `lint`); dependencies: `react`, `react-dom`, `axios`, `react-icons`, `vite`, `@vitejs/plugin-react`, `typescript`, eslint stack. Change `name` to e.g. `boardman-ui`. |
| `package-lock.json` | Regenerate with `npm install` after copy if preferred. |
| `tsconfig.json` | TS project for Vite + React. |
| `tsconfig.node.json` | Vite config typing. |
| `vite.config.ts` | Add `server.proxy`: `/api` → `http://localhost:8090` (or env). Change dev port if it clashes (Cyrex uses **5175**). |
| `index.html` | Root HTML; update `<title>` / meta to Board Manager. |
| `Dockerfile` | Multi-stage or dev pattern from Cyrex; align Node version with Cyrex. |
| `.eslintrc.cjs` | ESLint for `src`. |
| `.gitignore` | Node / dist / Vite defaults. |
| `public/favicon.svg` | Replace or keep as placeholder branding. |
| `src/main.tsx` | React root mount. |
| `src/vite-env.d.ts` | Vite client types. |
| `src/styles/variables.css` | Design tokens (colors, spacing) — keep for visual parity with Cyrex. |
| `src/context/AppProviders.tsx` | Provider wrapper. |
| `src/context/UIContext.tsx` | Active tab / UI state for sidebar. |
| `src/context/index.ts` | Re-exports. |
| `src/components/layout/Sidebar.tsx` | Nav rail; **strip** Cyrex tabs — e.g. one primary “Chat” (main panel) + optional Health; keep hook that opens MessagesWidget if Cyrex had a messages button in header/sidebar. |
| `src/components/layout/Sidebar.module.css` | Sidebar styles. |
| `src/App.css` | Global app layout (main + sidebar); keep, trim unused classes if any. |
| `src/utils/index.ts` | Shared helpers if still relevant after strip-down. |

#### Tier 2 — copy and adapt (Boardman-specific behavior)

| Source file | Purpose |
|-------------|---------|
| `src/components/MessagesWidget/MessagesWidget.tsx` | Floating **Messages** drawer (LinkedIn-style); adapt API calls from Cyrex agent/orchestration endpoints to **boardman** `POST /api/v1/agent/chat` (and session/history as implemented). Prune Cyrex-only flows (group chats, multi-agent factory) if you want a thinner v1 — keep the **UX**: list → thread, composer, loading states. |
| `src/components/MessagesWidget/MessagesWidget.css` | Widget layout and animations. |
| **`src/App.tsx` — extract only the Chat tab** | In Cyrex, the **“Interactive Chat”** block is under `activeTab === 'chat'` (starts ~line 3485): header + `renderConnectionPanel()`, scrollable **role-colored message bubbles**, input row, provider toggle (API vs local), Send, and optionally the **Local LLM Configuration** subsection below the box (~3667+). **Lift that JSX + the related state** (`chatHistory`, `chatInput`, `handleChatSend`, `ChatMessage` type, `chatProvider`, local LLM fields / `scanForLLMServices` if you still want local discovery — or replace local block with simple model dropdown fed by `boardman doctor` / env). Wire `handleChatSend` to **`/api/v1/agent/chat`** instead of `/orchestration/process`. **Do not** pull orchestration, workflow, RAG, or other tabs. |

#### Tier 3 — do **not** copy (explicit exclusions)

- **`src/components/AgentPlayground/**`** — entire folder, including **`LiveSpreadsheet.tsx` / `.css`** (no spreadsheet view).
- **`src/App.tsx` as a whole** — only the extracted Chat-tab slice (Tier 2); never drop the full file in as-is.
- **`src/components/VendorFraud/**`**, **`WorkflowPlayground/**`**, **`DocumentIndexing/**`** — Cyrex product surfaces.
- Any other Cyrex tabs/panels (orchestration forms, safety, intelligence playground, etc.).

#### Reference doc in Cyrex repo

- `deepiri-platform/diri-cyrex/cyrex-interface/README.md` — local dev (`npm run dev -- --port 5175`), Docker compose service name `cyrex-interface`.

#### Compose / ops

**Local dev:** Vite dev server (e.g. port **5176**) with `server.proxy` so the browser calls **`/api/*` → `http://localhost:8090`** (FastAPI). No nginx required day-to-day.

**Production (recommended): nginx + FastAPI**

- **FastAPI (uvicorn)** — API only: webhooks, REST, future `/api/v1/agent/*`. Listens internally on **8090** (not exposed publicly if nginx is the only entry).
- **nginx** — Terminates TLS (if used), serves the **static SPA** from `boardman-ui/dist` (`npm run build`), sets cache headers for assets; **`location /api/`** (and e.g. `/docs` if desired) **reverse-proxies** to the `boardman` upstream. SPA **fallback:** `try_files $uri /index.html` for client-side routing.
- **Optional multi-stage Docker:** build stage runs `npm ci && npm run build` for `boardman-ui`; final image is **nginx** + copied `dist/` + `default.conf`, alongside or separate from the **boardman** Python image — or one compose stack: services `boardman` (FastAPI), `boardman-nginx` (static + proxy), same Docker network (`proxy_pass http://boardman:8090`).

**Alternative (smaller deploys):** mount `StaticFiles` + catch-all route in FastAPI for `dist/` — one process, no nginx — acceptable if you do not need nginx features (rate limits, TLS termination at edge, separate static scaling).

---

## Implementation phases (unified)

**A — Organization layer**

- `repos.yml`; `issue_handler` / `pr_handler` read routing (Plaky table param or description fallback)
- `boardman register`

**B — Direction + scan**

- `boardman init`, `boardman scan`
- `boardman/llm/client.py` (or factory) — Claude + Ollama
- DB: `ScanRun`; `.env`: keys + `OLLAMA_BASE_URL`, `DEFAULT_SCAN_MODEL`

**C — Automated scan (optional / later)**

- Cron weekly scan; norozo/Discord summary; `boardman status`

**D — Agent prerequisites**

- `PROMPT_VERSION` documented above; confirm Plaky API for columns/subtasks/reorder

**E — Agent skeleton**

- `llm/factory.py` + Ollama + one cloud provider; LangChain + tools: `get_tasks`, `create_task` only; `boardman agent ask` (stateless)

**F — Agent memory**

- Alembic: `AgentSession`, `AgentMessage`, `ProjectContext`; CLI/API `session_id`; rolling history + optional summary

**G — Repo grounding**

- Local scanner + `DIRECTION.md` / README; optional GitHub fetch; structured `scan` tool output

**H — Plaky power tools**

- Extend `PlakyClient`; update title/body, subtasks, move/reorder per API; confirmation gate for bulk

**I — Polish**

- Optional LangGraph; background queue if scans timeout; `boardman doctor` (Ollama + model + Plaky checks)

**J — Web UI (Cyrex-derived)**

- New package `boardman-ui/`: Tier 1 + **MessagesWidget** + **extracted Interactive Chat panel** from Cyrex `App.tsx` only; **no** AgentPlayground / LiveSpreadsheet / spreadsheet; slim `App.tsx`; proxy to boardman `:8090`; Docker + compose optional.

---

## New files to create

```
deepiri-boardman/
├── pyproject.toml               # Poetry: dependencies + scripts (commit poetry.lock)
├── poetry.lock
├── poetry.toml                  # in-project .venv
├── repos.yml
├── boardman-ui/                 # Vite + React; Cyrex chat-only subset (see “Web UI”)
│   ├── package.json, tsconfig*.json, vite.config.ts, index.html
│   ├── Dockerfile, .eslintrc.cjs, .gitignore
│   ├── public/favicon.svg
│   └── src/
│       ├── main.tsx, App.tsx (slim: sidebar + chat + MessagesWidget toggle), App.css, vite-env.d.ts
│       ├── styles/variables.css
│       ├── context/
│       ├── components/layout/
│       ├── components/MessagesWidget/
│       ├── components/BoardmanChatPanel.tsx   # optional split: extracted Cyrex “Interactive Chat” tab
│       └── utils/
├── boardman/
│   ├── llm/
│   │   ├── __init__.py, factory.py, ollama.py, openai.py, anthropic.py, gemini.py
│   ├── agent/
│   │   ├── prompts.py, memory_store.py, guardrails.py, runner.py
│   │   └── tools/ ...
│   ├── services/
│   │   └── scan_handler.py
│   └── cli/
│       └── commands.py          # scan, init, register, status, agent, doctor
├── boardman/database/
│   └── models.py                # ScanRun, AgentSession, AgentMessage, ProjectContext
└── docs/
    ├── PLAN.md                  # This file
    ├── AGENT_PLAN.md
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

- [ ] Ollama works from container and host with documented ports
- [ ] Provider switch is env-only + restart
- [ ] Session survives restart (DB history)
- [ ] Agent does not invent task IDs (eval)
- [ ] Bulk Plaky change requires confirmation when flag on
- [ ] Scan/agent repo tools return real paths/excerpts
- [ ] `boardman-ui` dev server proxies API to boardman; agent chat round-trips in browser

---

## Document history

| Date | Change |
|------|--------|
| 2026-04-09 | UI/nginx plan; Python deps via **Poetry** (`pyproject.toml` + `poetry.lock`); Docker uses Poetry. |
