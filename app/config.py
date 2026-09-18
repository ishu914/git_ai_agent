import os
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict, DotEnvSettingsSource


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env."""

    app_name: str = Field(default="gitlab-ai-agent", alias="APP_NAME")
    ai_agent_host: str = Field(default="0.0.0.0", alias="AI_AGENT_HOST")
    ai_agent_port: int = Field(default=8000, alias="AI_AGENT_PORT")

    gitlab_url: str = Field(default="http://192.168.2.86", alias="GITLAB_URL")
    gitlab_token: Optional[str] = Field(default=None, alias="GITLAB_TOKEN")
    gitlab_webhook_secret: Optional[str] = Field(default=None, alias="GITLAB_WEBHOOK_SECRET")
    gitlab_webhook_signing_token: Optional[str] = Field(default=None, alias="GITLAB_WEBHOOK_SIGNING_TOKEN")
    gitlab_webhook_timestamp_tolerance_seconds: int = Field(default=300, alias="GITLAB_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS")

    openrouter_api_key: Optional[str] = Field(default=None, alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(default="openai/gpt-4o-mini", alias="OPENROUTER_MODEL")
    openrouter_enabled: bool = Field(default=True, alias="OPENROUTER_ENABLED")
    openrouter_model_strategy: str = Field(default="dynamic-free", alias="OPENROUTER_MODEL_STRATEGY")
    openrouter_free_model_max_attempts: int = Field(default=5, alias="OPENROUTER_FREE_MODEL_MAX_ATTEMPTS")
    openrouter_model_discovery_cache_ttl_seconds: int = Field(default=3600, alias="OPENROUTER_MODEL_DISCOVERY_CACHE_TTL_SECONDS")

    groq_api_key: Optional[str] = Field(default=None, alias="GROQ_API_KEY")
    groq_enabled: bool = Field(default=True, alias="GROQ_ENABLED")
    groq_model_strategy: str = Field(default="dynamic", alias="GROQ_MODEL_STRATEGY")
    groq_model_max_attempts: int = Field(default=3, alias="GROQ_MODEL_MAX_ATTEMPTS")
    groq_model_discovery_cache_ttl_seconds: int = Field(default=3600, alias="GROQ_MODEL_DISCOVERY_CACHE_TTL_SECONDS")
    groq_base_url: str = Field(default="https://api.groq.com/openai/v1", alias="GROQ_BASE_URL")

    ai_total_max_attempts: int = Field(default=8, alias="AI_TOTAL_MAX_ATTEMPTS")
    ai_model_cooldown_seconds: int = Field(default=300, alias="AI_MODEL_COOLDOWN_SECONDS")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        env_file = os.getenv("APP_ENV_FILE")
        if env_file:
            return (
                init_settings,
                env_settings,
                DotEnvSettingsSource(settings_cls, env_file=env_file),
                file_secret_settings,
            )
        return init_settings, env_settings, dotenv_settings, file_secret_settings


def get_settings() -> Settings:
    return Settings()
