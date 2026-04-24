"""Configuration module for alfred-coo-svc.

Loads from environment (/etc/alfred-coo/.env on Oracle, .env locally) via
pydantic-settings. Lists are accepted as either JSON arrays or as
comma-separated strings (friendlier for env files).
"""

from functools import lru_cache
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    soul_api_url: str = "http://100.105.27.63:8080"
    soul_api_urls: List[str] = [
        "http://100.105.27.63:8080",
        "https://soul-svc-1006583428928.us-central1.run.app",
    ]
    soul_api_key: str = ""
    soul_session_id: str = "alfred-coo"
    soul_node_id: str = "oracle"
    soul_harness: str = "alfred-coo-svc"
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    ollama_url: str = "http://172.17.0.1:8185/v1"
    # AB-21: all LLM traffic funnels through alfred-chat-stack gateway so the
    # concurrent trace middleware can capture it. `gateway_url` is the base
    # (no `/v1`); `_call_gateway` always hits `{gateway_url}/v1/chat/completions`.
    # If empty, dispatch falls back to deriving a base from `ollama_url`
    # (strip trailing `/v1`) so existing Oracle envs keep working unchanged.
    gateway_url: str = "http://172.17.0.1:8185"
    # Soul-key stamped in Authorization header for every gateway call. Shared
    # secret the gateway's allow-all policy will accept and the AB-21-gw trace
    # middleware will log. Empty = warn-and-continue (gateway still serves).
    autobuild_soulkey: str = ""
    # Tenant header value; fixed for the COO daemon. Overrideable for staging
    # but the production value must match the AB-21-gw pre-specified contract.
    tiresias_tenant: str = "alfred-coo-mc"
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
        case_sensitive=False,
    )

    @field_validator("soul_api_urls", mode="before")
    @classmethod
    def _split_urls(cls, v):
        if isinstance(v, str):
            return [u.strip() for u in v.split(",") if u.strip()]
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
