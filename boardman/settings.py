"""Pydantic-settings configuration (all env vars live here)."""

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Analysis/context limits, defined once (Sorge review, PR #88) --------------------
# These doubled as literals inside the modules that consume them ("or 16", "or 200"),
# so a change here silently disagreed with the fallback there. They now live in exactly
# one place: the Field default below *and* the module fallback both read these names.
DEFAULT_LLM_CONTEXT_BUDGET_CHARS = 24_000
DEFAULT_GITHUB_PR_MAX_FILES = 40
DEFAULT_GITHUB_PR_MAX_BODY_CHARS = 4_000
DEFAULT_GITHUB_CODE_SEARCH_MAX_FILES = 16
DEFAULT_GITHUB_CODE_SEARCH_MAX_BYTES_PER_FILE = 120_000
# Splitting open issues from open PRs costs one extra GitHub call per repo, so only the
# head of the activity ranking pays it. 8 covers the repos anyone actually asks about.
DEFAULT_GITHUB_ORG_ACTIVITY_SPLIT_TOP_N = 8
# 200 repo names is ~1.7KB of system prompt. Large orgs truncate with a visible note.
DEFAULT_AGENT_ORG_ROSTER_MAX_NAMES = 200
# Above this many characters a reply is substantive even if it contains "let me check",
# so the unfulfilled-preamble guard stops looking. See boardman/agent/runner.py.
DEFAULT_AGENT_PREAMBLE_MAX_CHARS = 600


def positive_or_default(raw: object, default: int) -> int:
    """Read a limit that slices a list or a string.

    Anything <= 0 (or unparseable) means "unset" and yields `default`. A negative would
    otherwise reach the wrong end of the sequence -- `names[:-1]` silently drops a real
    repo while the caller still reports the list as complete.
    """
    try:
        n = int(raw)  # type: ignore[call-overload]  # any object; TypeError is handled
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    plaky_api_key: str = ""
    plaky_api_base: str = "https://api.plaky.com/v1/public"
    # PR merged → Plaky status. Empty = resolve "Completed" from the board schema
    # (workflow_completed intent). Set a literal label/id to override.
    plaky_pr_merge_status: str = ""
    # When true, set `plaky_pr_merge_status` only after every linked PR is merged (or withdrawn).
    plaky_complete_when_all_prs_merged: bool = True
    # QA workflow (GitHub → Plaky). Empty = skip that transition (set to your board status keys).
    plaky_pr_needs_qa_status: str = ""
    plaky_pr_in_qa_status: str = ""
    plaky_pr_qa_approved_status: str = ""
    plaky_pr_qa_rejected_status: str = ""
    # Optional Plaky item field key for QA assignee (env PLAKY_QA_ITEM_FIELD_KEY). When set,
    # used before team_assignments.yml; when both empty, Boardman discovers a QA-ish person field from the board schema.
    plaky_qa_item_field_key: str = ""
    # Do not move draft PRs to Needs QA until ready_for_review (if needs_qa status is configured).
    plaky_skip_needs_qa_for_draft: bool = True
    # After any automated Plaky status change, enqueue SQLite job to reorder items in default board/group.
    plaky_reorder_after_status_change: bool = False
    # Comma-separated substrings (case-insensitive) marking Plaky item status as “done” for reorder heuristics.
    plaky_reorder_done_status_markers: str = "done,complete,closed,resolved,archive,shipped,merged"
    # Empty = resolve from board schema (dynamic_qa_status) when the matching plaky_pr_* value is also empty.
    plaky_status_needs_qa: str = ""
    plaky_status_in_qa: str = ""
    # Empty = resolve from Plaky board schema at runtime (see boardman.plaky.dynamic_qa_status).
    plaky_status_qa_approved: str = ""
    plaky_status_qa_rejected: str = ""
    plaky_status_completed: str = "completed"
    # PR-lifecycle statuses (empty = resolve from board schema via dynamic_qa_status intents):
    # assigned (dev matched/filled in), paused (comment said "pause"), in_progress (work resumed),
    # needs_qa_again (dev pinged QA after rework).
    plaky_status_assigned: str = ""
    plaky_status_paused: str = ""
    plaky_status_in_progress: str = ""
    plaky_status_needs_qa_again: str = ""
    # When True, on PR↔task link with no current assignee, fill the PR author as the engineer.
    plaky_pr_fill_assignee_from_author: bool = True
    plaky_pr_tracking_board_id: str = ""
    plaky_pr_tracking_group_id: str = ""
    # When true, GitHub PR link comments use HTML <a href> (Plaky often does not linkify bare URLs).
    plaky_pr_comment_links_as_html: bool = True
    # On startup, fetch the default board schema and fill blank `plaky_field_keys` in team_assignments.yml.
    plaky_auto_sync_team_assignment_field_keys: bool = True
    # Minimum interval between field-key sync attempts for the same board,
    # this is mainly for use if the board schema changes and it runs on startup to account for that.
    plaky_team_assignment_field_sync_cooldown_seconds: float = 60.0
    # Seconds; 0 disables TTL cache for fetch_board_schema_bundle
    plaky_board_schema_cache_ttl_seconds: float = 90.0
    # Read-only GitHub repo context reuse between questions. 0 disables the cache.
    github_read_cache_ttl_seconds: float = 300.0
    # Persistent planning-context snapshot TTL. The in-process GitHub cache is shorter;
    # this survives API/worker restarts without turning ProjectContext into a raw repo dump.
    agent_repo_context_cache_ttl_seconds: float = 900.0
    # Keep a stale snapshot available as a graceful fallback when GitHub is unavailable.
    agent_repo_context_stale_if_error_seconds: float = 86_400.0
    # Periodic knowledge sweep (worker-owned). This is a RECONCILIATION net for what the
    # webhooks missed, not a rescan: each cycle costs one cheap metadata call per repo and
    # only refetches a repo whose `pushed_at` moved since its snapshot was built.
    repo_knowledge_sweep_enabled: bool = True
    repo_knowledge_sweep_interval_seconds: float = 600.0
    # Bounded so one slow or broken repo cannot hold up the fleet.
    repo_knowledge_sweep_concurrency: int = 3
    repo_knowledge_sweep_max_repos: int = 25
    # Tunable analysis limits (Sorge review, PR #81): context budget for the repo
    # planning payload, PR review file cap, and code-search scope.
    llm_context_budget_chars: int = DEFAULT_LLM_CONTEXT_BUDGET_CHARS
    github_pr_max_files: int = DEFAULT_GITHUB_PR_MAX_FILES
    github_pr_max_body_chars: int = DEFAULT_GITHUB_PR_MAX_BODY_CHARS
    github_code_search_max_files: int = DEFAULT_GITHUB_CODE_SEARCH_MAX_FILES
    github_code_search_max_bytes_per_file: int = DEFAULT_GITHUB_CODE_SEARCH_MAX_BYTES_PER_FILE
    # How many of the busiest repos get their open issues split from their open PRs.
    # 0 means "make no extra calls"; a negative value asks for the default.
    github_org_activity_split_top_n: int = DEFAULT_GITHUB_ORG_ACTIVITY_SPLIT_TOP_N
    # Extra committed-artifact detections for repo hotspots, beyond the built-in list in
    # boardman/github/repo_hotspots.py. Format: "filename_suffix:why it matters",
    # SEMICOLON-separated, because reasons are prose and prose contains commas. A marker
    # is a filename ENDING of at least 3 characters (endswith, not substring). e.g.
    # "id_ed25519:private SSH key tracked in git;.tfstate:terraform state, with secrets".
    # Lets a deployment add a new sensitive file type without a code change.
    github_extra_artifact_rules: str = ""
    # QA GitHub-fit scoring knobs (see assignment/qa_picker.py for semantics).
    qa_fit_weight_direct: float = 0.0  # 0 = use the module default
    qa_fit_scoring_timeout_seconds: float = 0.0  # 0 = use the module default

    # --- Repo → Plaky placement auto-discovery (replaces repos.yml board/group IDs) ---
    # Catalog: all categorical boards + groups, cached on disk for webhook routing.
    plaky_catalog_cache_path: str = ".boardman/plaky-catalog.json"
    plaky_catalog_ttl_seconds: float = 86_400.0
    plaky_placement_auto_discover: bool = True
    plaky_placement_min_score: int = 400  # rank_plaky_rows threshold; see name_match.py
    # Limit search to the five categorical boards (excludes legacy AI Task Board, etc.).
    plaky_catalog_categorical_only: bool = True
    # Local "as-if-production" mode. When true, this instance polls GitHub for new activity
    # (issues, PRs, reviews, comments, pushes) on `testing_live_plaky_repos` and routes each
    # event through the same handlers as POST /api/v1/webhooks/github — so Plaky updates live
    # ONLY while this process runs. History from before startup is never replayed. Set false
    # in production, where real GitHub webhooks deliver events instead.
    testing_live_plaky: bool = False
    # Comma-separated owner/repo list, or "all"/"*" to watch every non-archived repo in
    # github_org that resolves to a Plaky board (github_poller.resolve_poller_repos).
    testing_live_plaky_repos: str = "Team-Deepiri/deepiri-boardman"
    testing_live_plaky_poll_seconds: float = 60.0
    # On startup the poller baselines existing events (no pre-start history replay). To avoid a
    # blind spot across restarts while testing, it also processes events created within this many
    # minutes of startup. 0 = strict baseline only. Duplicate issue tasks are still deduped by
    # IssueTaskMap. Irrelevant in production (TESTING_LIVE_PLAKY=false; real webhooks deliver events).
    testing_live_plaky_catchup_minutes: float = 45.0

    github_webhook_secret: str = ""
    # Production webhooks should acknowledge quickly and let boardman-worker run the
    # Plaky mutation.  Kept opt-in for local/dev callers that intentionally exercise
    # handlers inline without a worker.
    github_webhook_async_enabled: bool = False
    github_webhook_job_retries: int = 2
    github_reconcile_enabled: bool = False
    # The reconciliation sweep is a safety net for deliveries the webhook missed, not a
    # poller. Every cycle costs GitHub calls per registered repo, on the same rate limit
    # the agent's own tools spend. Lower it in .env for a local test, not here.
    github_reconcile_interval_seconds: float = 900.0
    github_reconcile_max_items: int = 50
    github_pat: str | None = None
    github_org: str = "deepiri-org"
    # Prepended to bare repo slugs (no "owner/") for QA roster + create-task; e.g. Team-Deepiri/foo.
    # When empty, falls back to github_org. github_org is still used for API org listing and routing.
    github_bare_repo_owner: str = "Team-Deepiri"
    # Org team for support roster: GET /api/v1/github/support-team/members (names/logins from GitHub)
    github_support_team: str = "Team-Deepiri/support-team"
    # List org teams (GET /orgs/{org}/teams) and parse tier from slug/name (qa-tier-3, t2-qa, …).
    # When false or no matching teams, Phase 1 uses activity-only inference.
    github_qa_tier_team_scan_enabled: bool = True
    github_skip_archived: bool = True
    # PR-search activity inference (sync_qa_capabilities Phase 1 fallback)
    github_qa_activity_half_life_days: float = 180.0
    github_qa_activity_search_max_pages: int = 5
    github_qa_activity_tier3_min_distinct_t3_repos: int = 2
    github_qa_activity_tier3_min_weighted_score: float = 5.0
    github_qa_activity_tier2_min_distinct_t2plus_repos: int = 3
    github_qa_activity_tier2_min_weighted_score: float = 2.5
    default_repo_category: str = ""
    default_plaky_table: str = ""
    # QA pick ranking from GitHub contribution profiles (cosine similarity vs the target
    # repo). False = legacy overlap-pool weighted-random pick only.
    qa_github_fit_enabled: bool = True

    database_url: str = "sqlite+aiosqlite:///./boardman.db"

    service_host: str = "0.0.0.0"
    service_port: int = 8090

    log_level: str = "INFO"

    repos_yml_path: str = "repos.yml"
    # QA/engineer Plaky field assignment (optional); see team_assignments.yml.example
    team_assignments_yml_path: str = "team_assignments.yml"
    # Written by sync_qa_capabilities.py; read by tier_classifier at runtime
    repo_signals_json_path: str = "repo_signals.json"

    # Ollama: leave llm_model empty to auto-pick from GET /api/tags (Docker-friendly).
    llm_provider: str = "ollama"
    llm_model: str = ""
    # Model to prefer when CPU-only mode is detected (no GPU).
    llm_ollama_cpu_model: str = "qwen2.5:0.5b"
    ollama_base_url: str = "http://localhost:11434"
    # Keep Ollama model loaded between requests (reduces cold-start latency). Examples: "30m", "-1" (forever)
    ollama_keep_alive: str = "30m"
    # Optional cap on generated tokens (Ollama options.num_predict). Unset = server default (often slow for long replies).
    ollama_num_predict: int | None = None
    # Provider-side retries for transient failures (429 TPM blips, 5xx). Without this a
    # momentary rate limit surfaces to the user as "I could not get a reply from the
    # language model" — i.e. a full outage from a 3-second hiccup.
    llm_max_retries: int = 4
    openai_api_key: str = ""
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_referer: str = ""
    openrouter_app_title: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""

    # Production scope: the worker-only deployment serves just the GitHub→Plaky webhook
    # automation. Set BOARDMAN_ENABLE_AGENT_API=false to NOT register the agent chat/scan
    # routes (no conversational routing / chat in production; UI is deployed separately).
    boardman_enable_agent_api: bool = True

    # PDF plan step 6: a tight recent window; 50 messages of prompt stuffing cost more
    # tokens than they added context. Raise per-deployment if a flow truly needs more.
    agent_max_history: int = 16
    agent_require_confirm_bulk: bool = True
    agent_langchain_tools: bool = True
    # LangGraph model↔tool steps cap (each step is often a full LLM call — keep low for latency)
    # 0 = mode-based ceiling (10 read / 16 write, PDF latency plan step 4); set to pin.
    agent_recursion_limit: int = 0
    # When True, LangChain AgentExecutor prints step traces (noisy; dev only)
    agent_langchain_verbose: bool = False
    # Repo names injected into every system prompt (see boardman/agent/org_roster.py).
    agent_org_roster_max_names: int = DEFAULT_AGENT_ORG_ROSTER_MAX_NAMES
    # Length ceiling for the unfulfilled-preamble guard (boardman/agent/runner.py). Raise
    # it if a model starts shipping longer bare promises; lower it if real short answers
    # are being retried. 0 = use DEFAULT_AGENT_PREAMBLE_MAX_CHARS.
    agent_preamble_max_chars: int = DEFAULT_AGENT_PREAMBLE_MAX_CHARS
    # Bumped when the system prompt changes shape, so sessions are never compared across
    # prompt generations. 2026-08-20: structured project state became the default context.
    prompt_version: str = "2026-08-20"

    cors_origins: str = (
        "http://localhost:5176,http://127.0.0.1:5176,"
        "http://localhost:8088,http://127.0.0.1:8088,http://localhost:3000"
    )

    # Shared with Cloudflare worker (Bearer) for POST /api/v1/assignment/pick-qa
    worker_internal_secret: str = ""

    # Gray-zone GitHub↔Plaky identity: optional LLM (Ollama recommended, temperature 0 in code)
    assignment_identity_llm_enabled: bool = False
    assignment_identity_llm_min_confidence: float = 0.82
    assignment_identity_llm_reject_below: float = 0.30
    assignment_identity_llm_gray_low: int = 380
    assignment_identity_llm_gray_high: int = 8200

    # PR ↔ Plaky fuzzy linking (pull_request.opened when no Fixes/Closes issue)
    pr_linking_pipeline_enabled: bool = True
    pr_linking_fetch_board_items: bool = True
    pr_linking_max_board_items_scan: int = 200
    pr_linking_board_max_pages: int = 10
    pr_linking_high_threshold: float = 90.0
    pr_linking_medium_threshold: float = 50.0
    pr_linking_top_n_for_llm: int = 5
    pr_linking_llm_enabled: bool = False
    pr_linking_llm_min_confidence: float = 0.75
    # Blend SequenceMatcher title/body score with word-bag cosine in [0, 1] (0 = legacy behavior only).
    pr_linking_cosine_weight: float = 0.35

    # POST /agent/chat with queue=true writes to SQLite `background_jobs` (requires boardman-worker).
    agent_async_enqueue_enabled: bool = True
    # Worker loop when no pending jobs (seconds).
    queue_worker_poll_seconds: float = 0.25
    # Jobs stuck in `running` longer than this are marked incomplete on worker startup.
    queue_worker_stale_running_seconds: int = 7200

    # Optional Redis for **API/agent** caching only (local dev or multi-replica). Leave empty in
    # production and for `boardman-worker` — the worker must not depend on Redis.
    agent_redis_url: str = ""

    # Leaky-bucket rate limit for POST /agent/chat and /agent/scan (per client IP)
    agent_rate_limit_enabled: bool = True
    agent_rate_limit_capacity: float = 16.0
    agent_rate_limit_leak_per_second: float = 0.5
    # When true, store bucket state in SQLite (`agent_rate_limit_buckets`) for multi-instance safety.
    # Also accepts legacy env AGENT_RATE_LIMIT_USE_REDIS.
    agent_rate_limit_use_sqlite: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "AGENT_RATE_LIMIT_USE_SQLITE",
            "AGENT_RATE_LIMIT_USE_REDIS",
        ),
    )


settings = Settings()
