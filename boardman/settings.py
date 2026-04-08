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

    database_url: str = "sqlite+aiosqlite:///./boardman.db"

    service_host: str = "0.0.0.0"
    service_port: int = 8090

    log_level: str = "INFO"


settings = Settings()