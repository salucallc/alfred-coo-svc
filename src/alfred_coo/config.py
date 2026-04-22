"""
Configuration module for Alfred Coo service settings.
"""

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    soul_api_url: str = "http://100.105.27.63:8080"
    soul_api_urls: List[str] = [
        "http://100.105.27.63:8080",
        "https://soul-svc-1006583428928.us-central1.run.app"
    ]
    soul_api_key: str = ""
    soul_session_id: str = "alfred-coo"
    soul_node_id: str = "oracle"
    soul_harness: str = "alfred-coo-svc"
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    ollama_url: str = "http://172.17.0.1:8185/v1"
    mesh_poll_interval_seconds: int = 30
    health_port: int = 8090
    log_level: str = "INFO"
    log_format: str = "json"
    slack_bot_token: str = ""
    slack_batcave_channel: str = "C0ASAKFTR1C"
    daily_budget_usd: float = 200.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
