
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    plaky_api_key: str = ""
    plaky_api_base: str = "https://api.plaky.com/v1/public"
    plaky_pr_merge_status: str = "in_review"
    # Plaky hierarchy: Item lives under Board + Group (no separate "table" in API)
    plaky_default_board_id: str = ""
    plaky_default_group_id: str = ""
    # Seconds; 0 disables TTL cache for fetch_board_schema_bundle
    plaky_board_schema_cache_ttl_seconds: float = 90.0

    github_webhook_secret: str = ""
    github_pat: str | None = None
    github_org: str = "deepiri-org"
    # Org team for support roster: GET /api/v1/github/support-team/members (names/logins from GitHub)
    github_support_team: str = "Team-Deepiri/support-team"
    github_skip_archived: bool = True
    default_repo_category: str = ""
    default_plaky_table: str = ""

    database_url: str = "sqlite+aiosqlite:///./boardman.db"

    service_host: str = "0.0.0.0"
    service_port: int = 8090

    log_level: str = "INFO"

    repos_yml_path: str = "repos.yml"
    # QA/engineer Plaky field assignment (optional); see team_assignments.yml.example
    team_assignments_yml_path: str = "team_assignments.yml"

    # Ollama: leave llm_model empty to auto-pick from GET /api/tags (Docker-friendly).
    llm_provider: str = "ollama"
    llm_model: str = ""
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

    # Redis: agent job queue (arq) + optional distributed leaky-bucket limits
    redis_url: str = ""
    # When REDIS_URL is set, POST /agent/chat may use async_enqueue=true (requires arq worker)
    agent_async_enqueue_enabled: bool = True

    # Leaky-bucket rate limit for POST /agent/chat and /agent/scan (per client IP)
    agent_rate_limit_enabled: bool = True
    agent_rate_limit_capacity: float = 16.0
    agent_rate_limit_leak_per_second: float = 0.5
    # Use Redis for the bucket when true and redis_url is set (multi-instance safe)
    agent_rate_limit_use_redis: bool = False


settings = Settings()
