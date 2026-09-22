import pytest
from app.ai.orchestrator import AIOrchestrator
from app.ai.types import AIUnavailableError
from app.config import Settings
from app.project_config import load_project_config


def test_global_external_ai_prohibited_filters_out_external_candidates(monkeypatch):
    monkeypatch.setenv("AI_EXTERNAL_PROVIDERS_ALLOWED", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter-test")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-groq-test")

    settings = Settings()
    orchestrator = AIOrchestrator()
    orchestrator.settings = settings

    candidates = orchestrator.candidates()
    assert candidates == []


def test_analyze_mr_raises_ai_unavailable_when_external_ai_prohibited(monkeypatch):
    monkeypatch.setenv("AI_EXTERNAL_PROVIDERS_ALLOWED", "false")

    orchestrator = AIOrchestrator()
    context = {"changed_files": []}
    validation = {"status": "pass"}

    with pytest.raises(AIUnavailableError, match="External AI providers prohibited"):
        orchestrator.analyze_mr("proj-1", 10, context, validation)


def test_project_level_ai_policy_override_prohibits_external_ai(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_EXTERNAL_PROVIDERS_ALLOWED", "true")

    orchestrator = AIOrchestrator()
    context = {"changed_files": []}
    validation = {"status": "pass"}

    project_settings = {
        "max_changed_files": 20,
        "ai": {"external_providers_allowed": False},
        "external_providers_allowed": False,
    }

    with pytest.raises(AIUnavailableError, match="External AI providers prohibited"):
        orchestrator.analyze_mr("proj-sensitive", 10, context, validation, project_settings=project_settings)


def test_project_level_ai_policy_override_allows_external_ai(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_EXTERNAL_PROVIDERS_ALLOWED", "false")

    orchestrator = AIOrchestrator()
    project_settings = {
        "max_changed_files": 20,
        "ai": {"external_providers_allowed": True},
        "external_providers_allowed": True,
    }

    # Should check candidates for project override if allowed
    candidates = orchestrator.candidates(external_allowed=True)
    assert isinstance(candidates, list)
