from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env."""

    app_name: str = Field(default="gitlab-ai-agent", alias="APP_NAME")
    ai_agent_host: str = Field(default="0.0.0.0", alias="AI_AGENT_HOST")
    ai_agent_port: int = Field(default=8000, alias="AI_AGENT_PORT")

    gitlab_url: str = Field(default="http://192.168.2.86", alias="GITLAB_URL")
    gitlab_token: Optional[str] = Field(default=None, alias="GITLAB_TOKEN")
    gitlab_webhook_secret: Optional[str] = Field(default=None, alias="GITLAB_WEBHOOK_SECRET")

    openrouter_api_key: Optional[str] = Field(default=None, alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(default="openai/gpt-4o-mini", alias="OPENROUTER_MODEL")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


def get_settings() -> Settings:
    return Settings()
