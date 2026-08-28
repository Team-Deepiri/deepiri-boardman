# Huddle origins

Every module in `boardman/planning/huddle/` was **taken from the standalone
[`deepiri-huddle`](../../../../deepiri-huddle) project** and integrated into
boardman in the *Wave 1 huddle integration* commit. This subpackage exists so
that boundary is explicit: it is the ported huddle planning engine, kept
separate from boardman's own meeting-plans layer one directory up
(`service.py`, `context_aggregator.py`, `team_config.py`, `team_models.py`).

## File-by-file mapping

| boardman module (`planning/huddle/`) | ported from (`deepiri-huddle/huddle/`) | notes |
|--------------------------------------|----------------------------------------|-------|
| `context_github.py` — `GitHubPlanningContext` | `github_feed.py` — `GitHubFeed` | PR feed → planning context; still carries the `_ClientCtx` wrapper from the original |
| `context_plaky.py` — `PlakyPlanningContext` | `plaky_feed.py` — `PlakyFeed` | Plaky board items → planning context |
| `context_sync.py` — `SyncPlanningContext` | huddle sync-feed logic | GitHub↔Plaky link/issue-map summaries |
| `context_direction.py` — `DirectionPlanningContext` | huddle direction/scan feed | `DIRECTION.md` + scan-run context |
| `llm_adapter.py` — `BoardmanPlanningLlm`, `PlanningLlm` | `llm.py` — `MultiProviderLlm`, `LlmResult` | multi-provider planning LLM |
| `planner.py` — `MeetingPlanner` | huddle planner + `planning_bridge.py` | prompt assembly + plan generation |
| `plan_output.py` — `validate_meeting_plan_markdown` | huddle plan-output validation | markdown section schema/validation |
| `models.py` — `MeetingRequest`, `MeetingPlan`, `TeamMeeting` | huddle planning models | shared planning dataclasses |
| `schedule.py` — `DEFAULT_TEAM_SCHEDULE` | huddle schedule config | default per-team meeting schedule |
| `team_repos.py` — `repos_for_team`, `load_team_repos` | huddle team-repo config | rewired to `planning.team_config` during integration |
| `team_plaky_boards.py` — `boards_for_team` | huddle team-board config | rewired to `planning.team_config` during integration |

## Integration seams (boardman-native, NOT huddle)

These live at `boardman/planning/` (one level up) and were written for boardman;
they sit on top of this subpackage:

- `service.py` — the meeting-plans service used by the REST route, CLI, and agent tool
- `context_aggregator.py` — orchestrates the four huddle context providers
- `team_config.py` / `team_models.py` — centralized team-config resolution the
  huddle `team_repos` / `team_plaky_boards` modules were rewired to delegate to
