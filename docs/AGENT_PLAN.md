# Deepiri Board Manager Agent — Module Reference

> **Status:** The agent layer described here is **largely implemented**. This doc supplements [PLAN.md](./PLAN.md) with module-level detail. For current CLI/API surfaces and repo map, see [AGENTS.md](../AGENTS.md).
>
> **Doc maintenance:** Update this file when agent modules, tools, or prompts change. See [AGENTS_MAINTENANCE.md](./AGENTS_MAINTENANCE.md).

## Vision

An AI-powered Product Manager that lives inside `deepiri-boardman`. It scans your repos, understands your goals, creates and manages Plaky tasks, and acts as an intelligent assistant for steering your project's direction.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          deepiri-boardman                              │
│                                                                         │
│  ┌─────────────┐    ┌──────────────┐    ┌──────────────────────────┐  │
│  │   REST API  │    │     CLI      │    │   LLM Client             │  │
│  │  (FastAPI)  │    │  (Typer)     │    │   (Ollama/Claude/etc)   │  │
│  └─────────────┘    └──────────────┘    └────────────┬─────────────┘  │
│                                                      │                 │
│  ┌─────────────┐    ┌──────────────┐    ┌───────────┴───────────┐  │
│  │  Database   │    │ Plaky Client │    │    Langchain Agent     │  │
│  │ (SQLAlchemy)│    │   (httpx)    │    │ ┌─────────────────────┐ │  │
│  └─────────────┘    └──────────────┘    │ │  System Prompt     │ │  │
│                                           │ │  + Memory (History)│ │  │
│                                           │ │  + Tools            │ │  │
│                                           └─────────────────────┘ │  │
│                                           └─────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Core Components (implemented)

### 1. LLM (`boardman/llm/`)

```
boardman/llm/
├── factory.py           # get_chat_model() — Ollama, OpenAI, Anthropic, Gemini, OpenRouter
├── completion.py        # Shared completion helpers
└── ollama_autodetect.py # Model pick from GET /api/tags when LLM_MODEL empty
```

Providers are LangChain chat models (not separate `ollama.py` / `openai.py` files).

### 2. LangChain Agent (`boardman/agent/`)

```
boardman/agent/
├── runner.py            # LangChain tool-calling agent (AgentExecutor / graph)
├── service.py           # run_agent_chat(), session orchestration
├── prompts.py           # BOARD_MANAGER_SYSTEM + prompt constants
├── guardrails.py        # allow_writes, bulk confirmation policy
├── memory_store.py      # AgentSession / AgentMessage persistence
├── task_draft.py        # Task draft state on session
├── tool_context.py      # Plaky board/group context for tools
└── tools/
    ├── plaky_tools.py       # Plaky list/create/update (gated by allow_writes)
    ├── repo_tools.py        # scan_local_repo, thoughts
    ├── github_tools.py      # GitHub read helpers
    └── assignment_tools.py  # QA assignment preview
```

---

## System Prompt (The "Brain")

```python
SYSTEM_PROMPT = """
You are the Deepiri Board Manager — an AI Product Manager that helps teams build software.

## Your Capabilities
- Scan repos to understand their structure and current state
- Create, update, and organize Plaky tasks
- Break down goals into actionable tasks
- Answer questions about project direction
- Suggest improvements and next steps
- Create subtasks for existing tasks
- Move tasks between columns/statuses
- Reorder tasks by priority

## How You Work
1. When asked to create tasks: understand the goal, break it down, create tasks in Plaky
2. When asked about direction: scan repo, analyze, recommend
3. When asked to organize: move tasks, create subtasks, reorder as needed
4. When asked to update: modify existing tasks, change status, update assignees

## Workflow
- If user gives a goal → ask clarifying questions if needed → create plan → create tasks
- If user asks for direction → scan repo → analyze → recommend
- If user asks to organize → understand the request → execute → confirm

## Guidelines
- Be concise but thorough
- Ask only necessary questions
- Always confirm before making big changes
- Remember conversation history for context
- Store important project context for future reference
- Be skeptical — verify info is current, don't assume outdated data

## Plaky Board Structure
- AI Bugs / What to DO: cyrex, sorge, any LLM/agent repos
- ML What to DO / Bugs: mudspeed, zepgpu, model research
- Infrastructure: norozo, boardman, docker/k8s work
- Backend / Database: platform core API, DB work
- Frontend: emotion-desktop, platform UI
- Joe Black's Ideas: raw ideas, not yet assigned to a repo

Route tasks to the appropriate table based on repo category.
"""
```

---

## Memory System (`boardman/agent/memory_store.py`)

- **Conversation history**: `AgentMessage` rows per `session_id` (limit `AGENT_MAX_HISTORY`)
- **Project context**: `ProjectContext` table — per-repo summary, goals, `last_scanned`
- **Session metadata**: `AgentSession` — `repo`, `prompt_version`, optional `task_draft_json`
- **Tool audit**: `tool_calls_json` on messages when tools run
- **SQLite** via SQLAlchemy async session; Alembic migration `002_agent_scan_tables.py`

---

## Tools (`boardman/agent/tools/`)

Built by `build_all_tools(allow_writes=…)` in `tools/__init__.py`:

| Module | Tools | Notes |
|--------|-------|-------|
| `plaky_tools.py` | list/get/create/update tasks, subtasks, comments | Writes require `allow_writes=true` |
| `repo_tools.py` | `scan_local_repo`, `thoughts` | Reads README, docs/, `DIRECTION.md`, `AGENTS.md`, bounded tree |
| `github_tools.py` | issue/file/commit reads | Read-mostly; no duplicate webhook logic |
| `assignment_tools.py` | `assignment_preview` | QA picker preview |

**Not separate files:** original design had `repo_scanner.py`, `plaky_tasks.py`, `plaky_board.py` — consolidated into `repo_tools.py` and `plaky_tools.py`.

---

## Database Models (Extended)

```python
# New tables for agent

class AgentSession(Base):
    __tablename__ = "agent_sessions"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True)
    repo: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_active: Mapped[datetime] = mapped_column(DateTime)


class AgentMessage(Base):
    __tablename__ = "agent_messages"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(20))  # "user", "assistant"
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProjectContext(Base):
    __tablename__ = "project_contexts"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo: Mapped[str] = mapped_column(String(255), unique=True)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    goals: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    last_scanned: Mapped[Optional[datetime]] = mapped_column(DateTime)
```

---

## CLI Commands (current)

```bash
# Agent chat (sticky session via --session)
poetry run boardman agent chat -m "What should we prioritize?" --repo owner/repo
poetry run boardman agent chat -m "Create a task for X" --allow-writes --use-tools

# Alias
poetry run boardman agent ask -m "List open Plaky tasks"

# Repo scan (separate from agent — LLM task generation)
poetry run boardman scan owner/repo --dry-run

# Health
poetry run boardman doctor
```

**Not implemented (original design):** `boardman agent --repo` REPL, `agent generate`, `agent scan`, `agent sessions`, `agent clear`, `boardman config set`. Use env vars (`LLM_PROVIDER`, `LLM_MODEL`) for provider config; `DELETE /api/v1/agent/sessions/{id}` to drop a session.

---

## API Endpoints (current)

```
POST   /api/v1/agent/chat              # message, session_id?, repo?, allow_writes?, queue?
POST   /api/v1/agent/chat/stream
GET    /api/v1/agent/jobs/{job_id}
GET    /api/v1/agent/sessions/{id}/history
DELETE /api/v1/agent/sessions/{id}
POST   /api/v1/agent/scan
POST   /api/v1/agent/init-direction
```

---

## Plaky Integration

**Shipped** (`boardman/plaky/client.py`):

- `create_task`, `get_tasks`, `get_task`, `add_comment`
- `update_task_fields`, `patch_item_field_values`, `create_subtask`
- Board schema: `list_boards`, `list_groups`, `get_board`, `list_board_items`
- Inventory helpers in `plaky/inventory.py`, placement in `plaky/placement.py`

**Partial / evolving:** column move, bulk reorder (`services/plaky_group_reorder.py` + worker job), assignee field updates via schema-driven patches.

**Not built:** dedicated `archive_task` wrapper; some original AGENT_PLAN method names map to `update_task_fields` / public items API instead.

---

## Agent Workflows

### Workflow 1: "Create tasks for X"

```
User: "Create tasks for adding user authentication"
Agent: 
  1. Ask: "What kind of auth? JWT, OAuth, both?"
  2. User: "JWT with refresh tokens"
  3. Agent scans repo (if not already scanned)
  4. Agent creates plan:
     - [High] Implement JWT login endpoint
     - [High] Add password hashing
     - [Medium] Create refresh token flow
     - [Medium] Add logout endpoint
     - [Low] Add OAuth2 optional
  5. Agent creates tasks in Plaky with repo tag
  6. Agent confirms: "Created 5 tasks in Plaky"
```

### Workflow 2: "What should we work on next?"

```
User: "What should we work on next for deepiri-platform?"
Agent:
  1. Scan repo (check recent commits, TODO, docs)
  2. Get current Plaky tasks status
  3. Analyze: what's blocked, what's in progress, what's done
  4. Recommend: "Based on your current state, I'd suggest..."
```

### Workflow 3: "Organize the board"

```
User: "Move all completed tasks to Done column, put high priority at top"
Agent:
  1. Get all tasks in Plaky
  2. Find completed tasks → move to Done
  3. Sort by priority within each column
  4. Confirm changes
```

### Workflow 4: "Create subtasks for task X"

```
User: "Break down the auth task into subtasks"
Agent:
  1. Get task details from Plaky
  2. Break into subtasks based on task description
  3. Create subtasks in Plaky
  4. Confirm
```

### Workflow 5: "Update assignee"

```
User: "Assign the login task to joe@deepiri.ai"
Agent:
  1. Find task by name or use provided ID
  2. Update assignee via Plaky API
  3. Confirm
```

---

## LLM Provider Support

```python
# Config in settings.py
llm_provider: str = "ollama"  # ollama, openai, anthropic, gemini
llm_model: str = "llama3:8b"
ollama_base_url: str = "http://ollama:11434"

# Optional: cloud providers
openai_api_key: str = ""
anthropic_api_key: str = ""
gemini_api_key: str = ""
```

---

## Docker Integration

```yaml
# docker-compose.yml
services:
  boardman:
    # ... existing config ...
    environment:
      - OLLAMA_BASE_URL=http://ollama:11434
      - LLM_PROVIDER=ollama
      - LLM_MODEL=llama3:8b
    depends_on:
      - ollama
  
  ollama:
    image: ollama/ollama
    container_name: deepiri-ollama
    ports:
      - "11434:11434"
    volumes:
      - ollama-data:/root/.ollama
    networks:
      - deepiri-network
```

---

## Implementation status

| Phase | Items | Status |
|-------|-------|--------|
| 1 — Foundation | LLM factory, LangChain agent, system prompt | **Done** |
| 2 — Memory | AgentSession, AgentMessage, ProjectContext | **Done** |
| 3 — Tools | repo_tools, plaky_tools, github_tools, assignment_tools | **Done** |
| 4 — CLI + API | `agent chat/ask`, `/api/v1/agent/*` | **Done** |
| 5 — Polish | History in prompts, guardrails, fallbacks, doctor | **Done** |

**v2+ (not started):** LangGraph state machine, vector memory, interactive REPL, `agent sessions` list CLI.

---

## Environment Variables

```bash
# LLM Configuration
LLM_PROVIDER=ollama
LLM_MODEL=llama3:8b
OLLAMA_BASE_URL=http://ollama:11434

# Optional: cloud providers
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GEMINI_API_KEY=

# Agent Configuration
AGENT_MAX_HISTORY=50
AGENT_SESSION_TIMEOUT=3600  # seconds
```

---

## Verification

```bash
# CLI agent
poetry run boardman agent chat -m "Summarize open Plaky tasks" --repo owner/repo

# API
curl -X POST http://localhost:8090/api/v1/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What is in DIRECTION.md?", "repo": "owner/repo"}'

# Session history (use session_id from chat response)
curl http://localhost:8090/api/v1/agent/sessions/{session_id}/history

# Automated tests
poetry run pytest tests/test_agent_guardrails.py tests/test_tools.py -q
```

---

## Future Enhancements

- **Voice input**: Talk to the agent
- **GitHub integration**: Auto-create issues from Plaky tasks
- **Notifications**: DM on Discord when tasks created
- **Scheduled reviews**: "Hey, review your board every Monday"
- **Multiple agents**: Different agents for different repos
- **AI direction generation**: Ask AI to suggest direction based on repo scan