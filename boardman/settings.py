from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    plaky_api_key: str = ""
    plaky_api_base: str = "https://api.plaky.com/v1/public"
    plaky_pr_merge_status: str = "in_review"
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
    plaky_pr_tracking_board_id: str = ""
    plaky_pr_tracking_group_id: str = ""
    # On startup, fetch the default board schema and fill blank `plaky_field_keys` in team_assignments.yml.
    plaky_auto_sync_team_assignment_field_keys: bool = True
    # Minimum interval between field-key sync attempts for the same board,
    # this is mainly for use if the board schema changes and it runs on startup to account for that.
    plaky_team_assignment_field_sync_cooldown_seconds: float = 60.0
    # Seconds; 0 disables TTL cache for fetch_board_schema_bundle
    plaky_board_schema_cache_ttl_seconds: float = 90.0

    github_webhook_secret: str = ""
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
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""

    agent_max_history: int = 50
    agent_require_confirm_bulk: bool = True
    agent_langchain_tools: bool = True
    # LangGraph model↔tool steps cap (each step is often a full LLM call — keep low for latency)
    agent_recursion_limit: int = 22
    # When True, LangChain AgentExecutor prints step traces (noisy; dev only)
    agent_langchain_verbose: bool = False
    prompt_version: str = "2026-04-09"

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
