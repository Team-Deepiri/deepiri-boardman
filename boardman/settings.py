from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    plaky_api_key: str = ""
    plaky_api_base: str = "https://api.plaky.com/v2"
    plaky_pr_merge_status: str = "in_review"

    github_webhook_secret: str = ""
    github_pat: Optional[str] = None
    github_org: str = "deepiri-org"
    github_skip_archived: bool = True
    default_repo_category: str = ""
    default_plaky_table: str = ""

    database_url: str = "sqlite+aiosqlite:///./boardman.db"

    service_host: str = "0.0.0.0"
    service_port: int = 8090

    log_level: str = "INFO"

    repos_yml_path: str = "repos.yml"

    llm_provider: str = "ollama"
    llm_model: str = "llama3:8b"
    ollama_base_url: str = "http://localhost:11434"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""

    agent_max_history: int = 50
    agent_require_confirm_bulk: bool = True
    agent_langchain_tools: bool = True
    prompt_version: str = "2026-04-09"

    cors_origins: str = (
        "http://localhost:5176,http://127.0.0.1:5176,"
        "http://localhost:8088,http://127.0.0.1:8088,http://localhost:3000"
    )


settings = Settings()