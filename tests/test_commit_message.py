from app.ai.commit_message import normalize_commit_message, select_commit_message, should_generate_commit_message
from app.ai.orchestrator import AIOrchestrator


def test_meaningful_user_commit_is_preserved():
    policy = {"enabled": True, "generate_when_default": True, "placeholders": ["Update"]}

    assert select_commit_message(["fix(webhook): preserve signed event handling"], "feat(fake): invented", policy) == "fix(webhook): preserve signed event handling"
    assert not should_generate_commit_message(["fix(webhook): preserve signed event handling"], policy)


def test_configured_placeholder_can_be_replaced_without_fabricated_metadata():
    policy = {"enabled": True, "generate_when_default": True, "placeholders": ["Update"]}

    assert should_generate_commit_message(["Update"], policy)
    assert select_commit_message(["Update"], "fix(webhook): prevent duplicate processing", policy) == "fix(webhook): prevent duplicate processing"


def test_commit_message_generation_rejects_secrets_and_issue_ids():
    assert normalize_commit_message("fix: use sk-secret-token") is None
    assert normalize_commit_message("fix: resolve JIRA-123") is None
    assert normalize_commit_message("fix: improve webhook validation") == "fix: improve webhook validation"


def test_disabled_commit_generation_preserves_existing_message():
    assert select_commit_message(["WIP"], "feat(fake): generated", {"enabled": False, "generate_when_default": True}) == "WIP"


def test_orchestrator_generates_only_for_configured_placeholder(monkeypatch, tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("APP_ENV_FILE", str(empty_env))
    orchestrator = AIOrchestrator()
    monkeypatch.setattr(orchestrator, "chat_completion", lambda *args, **kwargs: {"message": "fix(webhook): prevent duplicate events"})

    generated = orchestrator.generate_commit_message(
        [{"message": "Update"}],
        {"merge_request": {"title": "Prevent duplicates"}, "changed_files": [{"path": "app/api/webhook.py"}]},
        {"enabled": True, "generate_when_default": True, "placeholders": ["Update"]},
    )

    assert generated == "fix(webhook): prevent duplicate events"


def test_orchestrator_preserves_meaningful_commit_without_ai_call(monkeypatch, tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("APP_ENV_FILE", str(empty_env))
    orchestrator = AIOrchestrator()
    monkeypatch.setattr(orchestrator, "chat_completion", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("AI must not be called")))

    assert orchestrator.generate_commit_message(
        [{"message": "fix(auth): reject expired tokens"}],
        {"merge_request": {}, "changed_files": []},
        {"enabled": True, "generate_when_default": True},
    ) == "fix(auth): reject expired tokens"