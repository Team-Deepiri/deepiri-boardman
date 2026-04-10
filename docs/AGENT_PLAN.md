# Deepiri Board Manager Agent - Full Plan

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

## Core Components

### 1. LLM Client (`boardman/llm/`)

```
boardman/llm/
├── __init__.py
├── client.py          # Abstract LLM client interface
├── ollama.py          # Ollama implementation (from deepiri-platform)
├── openai.py          # OpenAI GPT implementation
├── anthropic.py       # Claude implementation
├── gemini.py          # Google Gemini implementation
└── factory.py        # Factory to get client based on config
```

### 2. Langchain Agent (`boardman/agent/`)

```
boardman/agent/
├── __init__.py
├── config.py          # Agent configuration, system prompt
├── prompt.py          # Main system prompt templates
├── memory.py          # Conversation history, session management
├── state.py           # Agent state (thinking, waiting, etc.)
├── tools/
│   ├── __init__.py
│   ├── repo_scanner.py      # Scan repo, read docs, code structure
│   ├── plaky_tasks.py       # Create, update, delete Plaky tasks
│   ├── plaky_board.py       # Move tasks, reorder, create subtasks
│   └── github_tools.py      # Create GitHub issues, PRs
└── agent.py                 # Main Langchain agent setup
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

## Memory System (`boardman/agent/memory.py`)

- **Conversation history**: Store last N messages per session
- **Project context**: Remember what each repo is about
- **Task history**: Remember what tasks were created for each repo
- **SQLite storage** for persistence

```python
class AgentMemory:
    def add_message(session_id, role, content)
    def get_history(session_id, limit=50)
    def store_project_context(repo, context)
    def get_project_context(repo)
    def clear_session(session_id)
```

---

## Tools System (`boardman/agent/tools/`)

### repo_scanner.py

```python
async def scan_repo(repo: str) -> dict:
    """Scan repo structure, read README, docs, find TODO"""
    # - List top-level directories
    # - Read README.md, docs/, SPEC.md
    # - Find TODO.md, CHANGELOG.md
    # - Get recent commit messages
    # - Return summary of repo state
```

### plaky_tasks.py

```python
async def create_task(title, description, priority, repo) -> dict
async def update_task(task_id, title, description, priority) -> dict
async def delete_task(task_id) -> dict
async def get_task(task_id) -> dict
async def list_tasks(repo, status) -> list
```

### plaky_board.py

```python
async def move_task_to_column(task_id, column_id) -> dict
async def create_subtask(parent_task_id, title) -> dict
async def get_task_details(task_id) -> dict
async def reorder_tasks(task_ids, column_id) -> dict
async def update_task_assignee(task_id, user_email) -> dict
async def get_columns(board_id) -> list
```

### github_tools.py

```python
async def create_issue(repo, title, body) -> dict
async def get_issues(repo, state) -> list
async def link_pr_to_issue(issue_number, pr_url) -> dict
```

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

## CLI Commands

```bash
# Start interactive agent session for a repo
boardman agent --repo deepiri-platform

# One-shot task generation
boardman agent generate --repo deepiri-platform --goal "Add user auth"

# Scan repo and summarize
boardman agent scan --repo deepiri-platform

# List agent sessions
boardman agent sessions

# Clear session history
boardman agent clear --session-id <id>

# Configure LLM provider
boardman config set llm_provider ollama
boardman config set llm_model llama3:8b
```

---

## API Endpoints

```python
# Agent chat endpoint
POST /api/v1/agent/chat
Body: {"message": "Create tasks for adding auth", "repo": "deepiri-platform"}
Response: {"ok": true, "reply": "...", "session_id": "..."}

# Agent sessions
GET /api/v1/agent/sessions
GET /api/v1/agent/sessions/{session_id}/history
DELETE /api/v1/agent/sessions/{session_id}

# Agent tools (direct calls)
POST /api/v1/agent/scan-repo
POST /api/v1/agent/create-tasks
POST /api/v1/agent/organize-board
```

---

## Plaky Integration (Extended)

Current: `create_task`, `add_comment`, `update_task_status`

New capabilities needed:
- **Get all columns** → understand board structure
- **Move task between columns** → PATCH with column ID
- **Create subtask** → POST /tasks/{id}/subtasks
- **Reorder tasks** → PATCH /tasks/reorder
- **Get task details** → including assignee, column, subtasks
- **Update assignee** → PATCH with assignee

```python
class PlakyClient:
    # ... existing methods ...
    
    async def get_columns(self, board_id: str = None) -> List[Dict]:
        """Get all columns in a board"""
    
    async def move_task_to_column(self, task_id: str, column_id: str) -> Dict:
        """Move task to different column"""
    
    async def create_subtask(self, parent_task_id: str, title: str) -> Dict:
        """Create subtask under a task"""
    
    async def get_task_details(self, task_id: str) -> Dict:
        """Get full task with subtasks, assignee, etc."""
    
    async def reorder_tasks(self, task_ids: List[str], column_id: str) -> Dict:
        """Reorder tasks in a column"""
    
    async def update_task_assignee(self, task_id: str, user_email: str) -> Dict:
        """Assign user to task"""
    
    async def archive_task(self, task_id: str) -> Dict:
        """Archive a task"""
```

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

## Implementation Order

### Phase 1: Foundation
1. Add LLM client factory + Ollama integration (copy from deepiri-platform)
2. Add basic Langchain agent setup
3. Add system prompt

### Phase 2: Memory
4. Add AgentSession, AgentMessage, ProjectContext models
5. Implement memory storage/retrieval
6. Add session management to agent

### Phase 3: Tools
7. Implement repo scanner tool
8. Extend Plaky client (columns, subtasks, reorder)
9. Implement plaky tasks tool
10. Implement plaky board tool
11. Implement github tools

### Phase 4: CLI + API
12. Add `boardman agent` CLI commands
13. Add `/api/v1/agent/` endpoints

### Phase 5: Polish
14. Add conversation history context to prompts
15. Add project context memory
16. Add error handling + fallbacks

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
# Test agent
boardman agent --repo deepiri-platform
> What would you like to work on?
> Create tasks for adding user auth
> What kind of auth do you need? (JWT, OAuth, etc)
> JWT with refresh tokens
> Created 5 tasks in Plaky

# Test API
curl -X POST http://localhost:8090/api/v1/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Create tasks for adding auth", "repo": "deepiri-platform"}'

# Test memory
boardman agent sessions

# Test organize
boardman agent --repo deepiri-platform
> Move high priority tasks to the top
```

---

## Future Enhancements

- **Voice input**: Talk to the agent
- **GitHub integration**: Auto-create issues from Plaky tasks
- **Notifications**: DM on Discord when tasks created
- **Scheduled reviews**: "Hey, review your board every Monday"
- **Multiple agents**: Different agents for different repos
- **AI direction generation**: Ask AI to suggest direction based on repo scan