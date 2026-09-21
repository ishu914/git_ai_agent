import pytest

from app.config import Settings


def test_validate_required_runtime_requires_gitlab_and_provider_credentials(monkeypatch):
    monkeypatch.setenv("GITLAB_URL", "http://gitlab.example")
    monkeypatch.setenv("GITLAB_TOKEN", "")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", "")
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("WORKER_DATABASE_PATH", "/var/lib/git-ai-reviewer/jobs.sqlite3")

    settings = Settings()

    with pytest.raises(ValueError, match="GITLAB_TOKEN is missing|OPENROUTER_API_KEY is missing|GROQ_API_KEY is missing"):
        settings.validate_required_runtime()


def test_validate_required_runtime_accepts_one_ai_provider(monkeypatch):
    monkeypatch.setenv("GITLAB_URL", "http://gitlab.example")
    monkeypatch.setenv("GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", "whsec_" + "a" * 20)
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("WORKER_DATABASE_PATH", "/var/lib/git-ai-reviewer/jobs.sqlite3")

    settings = Settings()
    settings.validate_required_runtime()
