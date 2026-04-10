from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    plaky_api_key: str = ""
    plaky_api_base: str = "https://api.plaky.com/v1/public"
    plaky_pr_merge_status: str = "in_review"
    # Plaky hierarchy: Item lives under Board + Group (no separate "table" in API)
    plaky_default_board_id: str = ""
    plaky_default_group_id: str = ""

    github_webhook_secret: str = ""
    github_pat: Optional[str] = None
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
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""

    agent_max_history: int = 50
    agent_require_confirm_bulk: bool = True
    agent_langchain_tools: bool = True
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


settings = Settings()