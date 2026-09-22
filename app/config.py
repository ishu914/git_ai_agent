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
    gitlab_ai_username: str = Field(default="gi_ai_code_reviewer", alias="GITLAB_AI_USERNAME")
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
    ai_max_input_tokens_per_request: int = Field(default=12000, alias="AI_MAX_INPUT_TOKENS_PER_REQUEST")
    ai_max_output_tokens_per_request: int = Field(default=1200, alias="AI_MAX_OUTPUT_TOKENS_PER_REQUEST")
    ai_max_total_tokens_per_mr: int = Field(default=6000, alias="AI_MAX_TOTAL_TOKENS_PER_MR")
    ai_max_diff_chars: int = Field(default=24000, alias="AI_MAX_DIFF_CHARS")
    ai_max_file_chars: int = Field(default=5000, alias="AI_MAX_FILE_CHARS")
    ai_max_context_files: int = Field(default=20, alias="AI_MAX_CONTEXT_FILES")
    ai_review_cache_ttl_seconds: int = Field(default=3600, alias="AI_REVIEW_CACHE_TTL_SECONDS")

    ai_max_concurrent_reviews: int = Field(default=2, ge=1, le=32, alias="AI_MAX_CONCURRENT_REVIEWS")
    max_concurrent_mr_jobs: int = Field(default=2, ge=1, le=32, alias="MAX_CONCURRENT_MR_JOBS")

    database_path: str = Field(default="data/events.sqlite3", alias="DATABASE_PATH")
    worker_database_path: str = Field(default="data/events.sqlite3", alias="WORKER_DATABASE_PATH")
    webhook_max_body_bytes: int = Field(default=1048576, alias="WEBHOOK_MAX_BODY_BYTES")
    webhook_max_concurrent_requests: int = Field(default=10, alias="WEBHOOK_MAX_CONCURRENT_REQUESTS")
    stale_processing_timeout_seconds: int = Field(default=300, alias="STALE_PROCESSING_TIMEOUT_SECONDS")
    event_max_attempts: int = Field(default=3, alias="EVENT_MAX_ATTEMPTS")
    event_retry_backoff_base_seconds: int = Field(default=5, alias="EVENT_RETRY_BACKOFF_BASE_SECONDS")
    event_retry_backoff_max_seconds: int = Field(default=300, alias="EVENT_RETRY_BACKOFF_MAX_SECONDS")
    event_retention_days: int = Field(default=90, alias="EVENT_RETENTION_DAYS")
    shutdown_timeout_seconds: int = Field(default=15, alias="SHUTDOWN_TIMEOUT_SECONDS")
    ai_external_providers_allowed: bool = Field(default=True, alias="AI_EXTERNAL_PROVIDERS_ALLOWED")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        populate_by_name=True,
        extra="ignore",
    )

    def validate_required_runtime(self) -> None:
        """Fail clearly when the application cannot run in production."""
        if not self.gitlab_url:
            raise ValueError("GITLAB_URL is missing")
        if not self.gitlab_token:
            raise ValueError("GITLAB_TOKEN is missing")
        if not self.gitlab_webhook_signing_token and not self.gitlab_webhook_secret:
            raise ValueError("GITLAB_WEBHOOK_SIGNING_TOKEN is missing")
        if not self.openrouter_api_key and not self.groq_api_key:
            raise ValueError("OPENROUTER_API_KEY is missing and GROQ_API_KEY is missing")

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
