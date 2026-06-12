# New Features Plan

> **Doc maintenance:** Update status columns when features ship or partial implementations land. See [AGENTS_MAINTENANCE.md](./AGENTS_MAINTENANCE.md).

## Overview

Extend deepiri-boardman with bidirectional sync and additional workflow automation. Items below were drafted before the agent/scan layer shipped; statuses reflect the repo as of 2026-06-05.

| # | Feature | Status |
|---|---------|--------|
| 1 | Bidirectional Plaky → GitHub | **Planned** |
| 2 | QA reviewer → In QA status | **Partial** — `pr_handler.py` when `PLAKY_PR_IN_QA_STATUS` configured |
| 3 | Dev assignee column | **Partial** — field patches via `update_task_fields`; no dedicated `assign_task` |
| 4 | CLI parse markdown → tasks | **Superseded** — use `boardman scan` / `POST /api/v1/agent/scan` |
| 5 | PR opened → Needs QA | **Partial** — `_maybe_set_needs_qa` in `pr_handler.py` when env configured |
| 6 | PR merged → Complete | **Done** — configurable via `PLAKY_PR_MERGE_STATUS` (default `in_review`) |
| 7 | Consolidated direction data | **Done** — canonical `DIRECTION.md` per repo + optional `ProjectContext` cache |

---

## 1. Bidirectional Sync: Plaky → GitHub — **Planned**

**Problem**: Currently only GitHub → Plaky works. Need to create GitHub issues from Plaky tasks.

**Implementation**:
- Add `POST /api/v1/webhooks/plaky` endpoint for Plaky webhooks
- When Plaky task created/updated → create/update GitHub issue via GitHub API
- Store mapping: `plaky_task_id ↔ github_issue_number`
- Use GitHub PAT for API calls

**Route**: `boardman/routes/plaky_events.py`

---

## 2. QA Reviewer Column → "In QA" Status — **Partial**

**Problem**: When a PR is opened, task should go to "Needs QA". When QA reviewer is assigned, it should go to "In QA".

**Shipped**: `boardman/services/pr_handler.py` sets In QA on review-requested events when `PLAKY_PR_IN_QA_STATUS` or dynamic schema resolution is configured.

**Remaining**: Ensure all board schemas map reviewer-added events reliably; document required Plaky status keys per board.

---

## 3. Dev Assignee Column — **Partial**

**Problem**: Need to assign developers to Plaky tasks.

**Shipped**: `patch_item_field_values` / `update_task_fields` can set person fields when schema keys are known (`team_assignments.yml`).

**Remaining**: Dedicated `assign_task(task_id, user_email)` helper; auto-assign from GitHub issue assignee on webhook.

---

## 4. CLI: Parse Markdown/Changelog → Create Plaky Tasks — **Superseded**

**Was**: `boardman generate --repo … --source ./direction.md`

**Now**: `boardman scan owner/repo` and `POST /api/v1/agent/scan` read `DIRECTION.md` via GitHub API, use LLM to propose tasks, dedupe via `IssueTaskMap` / scan logic.

The brainstorming formats below remain useful if a **non-LLM** deterministic parser is added later.

---

## 5. PR Opened → "Needs QA" Status — **Partial**

**Shipped**: `handle_pr_opened` calls `_maybe_set_needs_qa` when `PLAKY_PR_NEEDS_QA_STATUS` (or dynamic `plaky_status_needs_qa`) is set. Skips draft PRs when `PLAKY_SKIP_NEEDS_QA_FOR_DRAFT=true`.

**Remaining**: Default-on behavior per board without manual env tuning.

---

## 6. PR Merged → "Complete" — **Done**

Configurable via `PLAKY_PR_MERGE_STATUS` (default `in_review`). Set to board-specific "done" key when ready.

---

## 7. Consolidated Direction Data — **Done**

**Canonical:** `DIRECTION.md` at repo root (template: [DIRECTION_TEMPLATE.md](./DIRECTION_TEMPLATE.md)).

**Cache:** `ProjectContext` table updated after scan/agent operations.

**Also supported in scans:** README, `docs/`, open GitHub issues, recent commits.

---

# Brainstorming: CLI Task Generation (archived)

> Superseded by `boardman scan` for LLM-based generation. Kept for reference if a regex/YAML parser is added.

## Concept

User runs: `boardman generate --repo deepiri-platform --source ./direction.md`

## Data Formats Supported

### Option A: Markdown Task List
```markdown
# Platform Direction Q2 2024

## Backend
- [ ] Implement user authentication
- [ ] Add API rate limiting
```

### Option B: YAML Config (`.boardman.yaml`)
```yaml
direction: "Q2 2024 Platform Improvements"
tasks:
  - title: "User Authentication"
    priority: high
```

### Option C: Issue/Feature List from GitHub Issues
```bash
boardman generate --repo deepiri-platform --from-github --label "to-do"
```

---

# Implementation Order (updated)

1. ~~**PR → "Needs QA" status**~~ — partial; configure env per board
2. ~~**QA reviewer → "In QA" status**~~ — partial; see §2
3. ~~**CLI generate command**~~ — superseded by `boardman scan`
4. **Plaky → GitHub webhooks** — still planned
5. **Dev assignee automation** — partial; extend §3

---

# Open Questions

1. ~~Where should "direction" data live?~~ → `DIRECTION.md` per repo (resolved)
2. Should we support non-LLM deterministic parsing? (optional `boardman generate`)
3. How to handle task dependencies?
4. What's the naming convention for generated tasks?
