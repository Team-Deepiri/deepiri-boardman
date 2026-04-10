# New Features Plan

## Overview

Extend deepiri-boardman to support bidirectional sync and intelligent task generation from consolidated project data.

---

## 1. Bidirectional Sync: Plaky → GitHub

**Problem**: Currently only GitHub → Plaky works. Need to create GitHub issues from Plaky tasks.

**Implementation**:
- Add `POST /api/v1/webhooks/plaky` endpoint for Plaky webhooks
- When Plaky task created/updated → create/update GitHub issue via GitHub API
- Store mapping: `plaky_task_id ↔ github_issue_number`
- Use GitHub PAT for API calls

**Route**: `boardman/routes/plaky_events.py`

---

## 2. QA Reviewer Column → "In QA" Status

**Problem**: When a PR is opened, task should go to "Needs QA". When QA reviewer is assigned, it should go to "In QA".

**Implementation**:
- GitHub PR webhook detects `reviewers` added to PR
- Look up linked Plaky task
- Call `plaky.update_task_status(task_id, "in_qa")`
- Add new status mapping in settings: `plaky_pr_needs_qa_status=needs_qa`, `plaky_pr_in_qa_status=in_qa`

**Files**: `boardman/services/pr_handler.py` - extend `handle_pr_opened` to set initial "needs_qa" status

---

## 3. Dev Assignee Column

**Problem**: Need to assign developers to Plaky tasks.

**Implementation**:
- Extend PlakyClient: `assign_task(task_id, user_email)` 
- PATCH `/tasks/{id}` with `{"assignee": "..."}`
- GitHub issue handler can pull assignee from GitHub user and map to Plaky

**Files**: `boardman/plaky/client.py`

---

## 4. CLI: Parse Markdown/Changelog → Create Plaky Tasks

**Problem**: User wants to point CLI at a repo's "direction" data (markdown, changelog, spec) and auto-generate Plaky tasks.

**Approach**: See brainstorming section below.

**Files**: `boardman/cli/commands.py` - add `generate` command

---

## 5. PR Opened → "Needs QA" Status

**Current**: Adds PR URL as comment  
**New**: Move task to "Needs QA" column/status

**Implementation**:
- In `handle_pr_opened`: call `plaky.update_task_status(task_id, settings.plaky_pr_needs_qa_status)`

**Files**: `boardman/services/pr_handler.py`

---

## 6. PR Merged → "Complete"

**Current**: Already implemented (→ "in_review")  
**Update**: Make configurable to "done" instead

---

## 7. Consolidated Direction Data

**Problem**: Where does the "direction" data live?

**Options**:
1. **Markdown files** in repo: `SPEC.md`, `CHANGELOG.md`, `TODO.md`
2. **YAML/JSON config**: `.boardman.yaml` in repo root
3. **Database table**: `ProjectDirection` - stores goals per repo

---

# Brainstorming: CLI Task Generation

## Concept

User runs: `boardman generate --repo deepiri-platform --source ./direction.md`

System parses `direction.md`, extracts tasks, creates Plaky tasks.

## Data Formats Supported

### Option A: Markdown Task List
```markdown
# Platform Direction Q2 2024

## Backend
- [ ] Implement user authentication
- [ ] Add API rate limiting
- [ ] Set up PostgreSQL connection

## Frontend
- [ ] Build login page
- [ ] Create dashboard UI
```

### Option B: YAML Config (`.boardman.yaml`)
```yaml
direction: "Q2 2024 Platform Improvements"
tasks:
  - title: "User Authentication"
    description: "Implement JWT-based auth"
    priority: high
  - title: "API Rate Limiting"
    priority: medium
```

### Option C: Issue/Feature List from GitHub Issues
```bash
boardman generate --repo deepiri-platform --from-github --label "to-do"
```
Fetches all GitHub issues with label "to-do" → creates Plaky tasks.

## Parsing Strategy

1. **CLI loads file** (markdown or YAML)
2. **Extract tasks** using regex or structured parsing
3. **Deduplicate** against existing IssueTaskMap
4. **Create Plaky tasks** in batch with proper formatting
5. **Store mapping** in database

## Output

```
$ boardman generate --repo deepiri-platform --source ./roadmap.md
Found 12 tasks in roadmap.md
Creating Plaky tasks...
  ✓ User Authentication (high)
  ✓ API Rate Limiting (medium)
  - Skipped: Dashboard UI (already exists)
Created 10 tasks, skipped 2
```

## Future: AI-Assisted Generation

Use Gemini API to parse free-form text:
```
boardman generate --repo deepiri-platform --ai --prompt "Parse our Discord discussion about Q2 goals"
```

---

# Implementation Order

1. **PR → "Needs QA" status** - quick win
2. **QA reviewer → "In QA" status** - extends #1
3. **CLI generate command** - markdown/YAML parsing
4. **Plaky → GitHub webhooks** - reverse sync
5. **Dev assignee** - extends #4

---

# Open Questions

1. Where should "direction" data live? (file in repo vs centralized DB)
2. Should we support AI parsing? (Gemini API)
3. How to handle task dependencies?
4. What's the naming convention for generated tasks?