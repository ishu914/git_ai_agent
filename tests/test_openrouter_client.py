import os
from typing import Any

import pytest

from app.ai.client import OpenRouterClient
from app.ai.code_reviewer import review_merge_request
from app.ai.model_discovery import ModelDiscoveryCache, candidates_from_catalog
from app.ai.orchestrator import AIOrchestrator
from app.ai.types import AIModelCandidate, AIUnavailableError


class FakeResponse:
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _client() -> OpenRouterClient:
    return OpenRouterClient(api_key="test-key", model="openrouter/free", max_retries=0)


def _completion(content: Any, finish_reason: str = "stop") -> FakeResponse:
    return FakeResponse(200, {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]})


def test_client_uses_environment_configuration(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")

    client = OpenRouterClient()

    assert client.api_key == "test-key"
    assert client.model == "openai/gpt-4o-mini"
    assert client.base_url == "https://openrouter.ai/api/v1"
    assert client.timeout == 30


def test_client_builds_chat_payload():
    client = OpenRouterClient(api_key="demo-key", model="openai/gpt-4o-mini")

    payload = client.build_chat_payload(
        messages=[{"role": "user", "content": "Hello"}],
        temperature=0.2,
    )

    assert payload["model"] == "openai/gpt-4o-mini"
    assert payload["temperature"] == 0.2
    assert payload["messages"][0]["content"] == "Hello"
    assert "response_format" in payload


def test_client_raises_for_missing_api_key(monkeypatch, tmp_path):
    empty_env = tmp_path / ".env.empty"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("APP_ENV_FILE", str(empty_env))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterClient(api_key=None)


def test_chat_completion_parses_json_string(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: _completion('{"summary":"ok"}'))

    assert client.chat_completion([]) == {"summary": "ok"}


def test_chat_completion_returns_dict_content(monkeypatch):
    client = _client()
    result = {"summary": "already parsed"}
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: _completion(result))

    assert client.chat_completion([]) == result


def test_chat_completion_rejects_empty_content(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: _completion(None))

    with pytest.raises(ValueError, match="empty content"):
        client.chat_completion([])


def test_chat_completion_rejects_missing_choices(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: FakeResponse(200, {}))

    with pytest.raises(ValueError, match="choices"):
        client.chat_completion([])


def test_chat_completion_rejects_missing_message(monkeypatch):
    client = _client()
    response = FakeResponse(200, {"choices": [{"finish_reason": "stop"}]})
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: response)

    with pytest.raises(ValueError, match="message"):
        client.chat_completion([])


def test_chat_completion_rejects_malformed_json(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: _completion("not json"))

    with pytest.raises(ValueError, match="malformed JSON"):
        client.chat_completion([])


def test_chat_completion_parses_json_code_fence(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: _completion('```json\n{"summary":"fenced"}\n```'))

    assert client.chat_completion([]) == {"summary": "fenced"}


def test_chat_completion_raises_controlled_api_error(monkeypatch):
    client = _client()
    response = FakeResponse(500, {"error": {"message": "temporary upstream failure"}})
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: response)

    with pytest.raises(RuntimeError, match="status 500"):
        client.chat_completion([])


def test_chat_completion_retries_without_response_format(monkeypatch):
    client = _client()
    responses = [
        FakeResponse(400, {"error": {"message": "response_format unsupported"}}),
        _completion('{"summary":"fallback"}'),
    ]
    payloads = []

    def fake_request(method, path, payload):
        payloads.append(payload)
        return responses.pop(0)

    monkeypatch.setattr(client, "_request", fake_request)

    assert client.chat_completion([]) == {"summary": "fallback"}
    assert "response_format" in payloads[0]
    assert "response_format" not in payloads[1]


def test_chat_completion_rejects_incomplete_response(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: _completion('{"summary":"partial"}', "length"))

    with pytest.raises(ValueError, match="incomplete"):
        client.chat_completion([])


def test_code_review_failure_is_unavailable():
    class FailingClient:
        def chat_completion(self, **kwargs):
            raise RuntimeError("AI unavailable")

    result = review_merge_request(FailingClient(), {"merge_request": {}, "changes": {"changes": []}})

    assert result == {
        "status": "unavailable",
        "summary": "AI review could not be completed.",
        "findings": [],
    }


def test_model_catalog_filters_non_free_models():
    catalog = {
        "data": [
            {"id": "free/model:free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "paid/model", "pricing": {"prompt": "1", "completion": "1"}},
        ]
    }

    candidates = candidates_from_catalog("openrouter", catalog, free_only=True)

    assert [candidate.model_id for candidate in candidates] == ["free/model:free"]


def test_model_discovery_cache_avoids_repeated_fetches():
    cache = ModelDiscoveryCache()
    calls = {"count": 0}

    def fetch():
        calls["count"] += 1
        return [AIModelCandidate("openrouter", "free/model:free")]

    cache.get_or_fetch("test", 3600, fetch)
    cache.get_or_fetch("test", 3600, fetch)

    assert calls["count"] == 1


def test_orchestrator_falls_back_to_next_model(monkeypatch, tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("APP_ENV_FILE", str(empty_env))
    monkeypatch.setenv("OPENROUTER_API_KEY", "configured-for-test")
    monkeypatch.setenv("AI_MODEL_COOLDOWN_SECONDS", "300")
    orchestrator = AIOrchestrator()
    candidates = [
        AIModelCandidate("openrouter", "first:free"),
        AIModelCandidate("openrouter", "second:free"),
    ]
    calls = []

    class FakeClient:
        def __init__(self, model):
            self.model = model

        def chat_completion(self, *args, **kwargs):
            calls.append(self.model)
            if self.model == "first:free":
                raise RuntimeError("429 quota exhausted")
            return {"summary": "success"}

    monkeypatch.setattr(orchestrator, "candidates", lambda: candidates)
    monkeypatch.setattr(orchestrator, "_client_for", lambda candidate: FakeClient(candidate.model_id))

    assert orchestrator.chat_completion([]) == {"summary": "success"}
    assert calls == ["first:free", "second:free"]


def test_orchestrator_reports_unavailable_without_credentials(monkeypatch, tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("APP_ENV_FILE", str(empty_env))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    orchestrator = AIOrchestrator()

    with pytest.raises(AIUnavailableError):
        orchestrator.chat_completion([])


def test_orchestrator_uses_groq_after_openrouter_failure(monkeypatch, tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("APP_ENV_FILE", str(empty_env))
    monkeypatch.setenv("AI_TOTAL_MAX_ATTEMPTS", "2")
    orchestrator = AIOrchestrator()
    candidates = [
        AIModelCandidate("openrouter", "free/model:free"),
        AIModelCandidate("groq", "groq/model"),
    ]

    class FakeClient:
        def __init__(self, provider):
            self.provider = provider

        def chat_completion(self, *args, **kwargs):
            if self.provider == "openrouter":
                raise RuntimeError("provider unavailable")
            return {"summary": "groq success"}

    monkeypatch.setattr(orchestrator, "candidates", lambda: candidates)
    monkeypatch.setattr(orchestrator, "_client_for", lambda candidate: FakeClient(candidate.provider))

    assert orchestrator.chat_completion([]) == {"summary": "groq success"}
