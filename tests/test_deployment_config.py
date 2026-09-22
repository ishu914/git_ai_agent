import pytest
from pathlib import Path

from app.config import Settings


def test_validate_required_runtime_requires_gitlab_and_provider_credentials(monkeypatch):
    monkeypatch.setenv("GITLAB_URL", "http://gitlab.example")
    monkeypatch.setenv("GITLAB_TOKEN", "")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", "")
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "")

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

    settings = Settings()
    settings.validate_required_runtime()


def test_active_systemd_deployment_is_single_process():
    root = Path(__file__).resolve().parents[1]
    service = (root / "deploy" / "systemd" / "git-ai-agent.service").read_text(encoding="utf-8")
    deployment = (root / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")

    assert "ExecStart=/opt/git-ai-reviewer/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000" in service
    assert "Restart=on-failure" in service
    assert "User=git-ai-reviewer" in service
    assert "ProtectSystem=strict" in service
    assert "git-ai-agent.service" in deployment
    assert "git-ai-worker.service" not in deployment
    assert "WORKER_DATABASE_PATH" not in deployment


def test_runtime_environment_template_has_no_worker_settings():
    root = Path(__file__).resolve().parents[1]
    env_example = (root / ".env.example").read_text(encoding="utf-8")

    assert "WORKER_" not in env_example
    assert "GITLAB_TOKEN=" in env_example
    assert "OPENROUTER_API_KEY=" in env_example
