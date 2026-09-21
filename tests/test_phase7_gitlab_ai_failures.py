import httpx
import pytest

from app.ai.client import (
    OpenRouterAPIError,
    OpenRouterClient,
    OpenRouterEmptyContentError,
    OpenRouterIncompleteResponseError,
    OpenRouterMalformedJSONError,
)
from app.ai.model_discovery import candidates_from_catalog
from app.ai.orchestrator import AIOrchestrator, MODEL_COOLDOWNS, PROVIDER_COOLDOWNS
from app.ai.types import AIModelCandidate
from app.gitlab.client import GitLabClient


class FakeGitLabResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeOpenRouterResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _gitlab_client():
    return GitLabClient(base_url="http://gitlab.example", token="gitlab-pat", max_retries=1)


def test_gitlab_client_retries_retryable_statuses(monkeypatch):
    calls = []
    client = _gitlab_client()

    def fake_request(**kwargs):
        calls.append(kwargs["url"])
        if len(calls) == 1:
            return FakeGitLabResponse(429, {"message": "rate limited"})
        return FakeGitLabResponse(200, {"id": 42, "name": "demo"})

    monkeypatch.setattr("httpx.request", fake_request)
    result = client.get_project("group/project")

    assert result == {"id": 42, "name": "demo"}
    assert len(calls) == 2
    assert "group%2Fproject" in calls[1]


def test_gitlab_client_treats_auth_and_not_found_as_permanent(monkeypatch):
    client = _gitlab_client()
    for status in (401, 403, 404, 409):
        calls = {"count": 0}

        def fake_request(**kwargs):
            calls["count"] += 1
            return FakeGitLabResponse(status, {"message": "issue"})

        monkeypatch.setattr("httpx.request", fake_request)
        with pytest.raises(RuntimeError, match=f"status {status}"):
            client.get_project("group/project")
        assert calls["count"] == 1


def test_gitlab_client_retries_500_502_503_and_timeout_errors(monkeypatch):
    client = _gitlab_client()
    calls = []

    def fake_request(**kwargs):
        calls.append(kwargs["url"])
        if len(calls) == 1:
            return FakeGitLabResponse(503, {"message": "temporarily unavailable"})
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr("httpx.request", fake_request)
    with pytest.raises(httpx.TimeoutException):
        client.get_project("group/project")
    assert len(calls) == 2

    def connect_error(**kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("httpx.request", connect_error)
    with pytest.raises(httpx.ConnectError):
        client.get_project("group/project")


def test_gitlab_client_does_not_expose_raw_bodies_or_tokens(monkeypatch):
    client = _gitlab_client()

    def fake_request(**kwargs):
        return FakeGitLabResponse(500, {"error": {"message": "password=super-secret-token"}})

    monkeypatch.setattr("httpx.request", fake_request)
    with pytest.raises(RuntimeError) as excinfo:
        client.get_project("group/project")

    message = str(excinfo.value)
    assert "super-secret-token" not in message
    assert "gitlab-pat" not in message
    assert "status 500" in message


def test_gitlab_client_rejects_malformed_json_and_missing_fields(monkeypatch):
    client = _gitlab_client()

    def malformed(**kwargs):
        return FakeGitLabResponse(200, ValueError("bad json"))

    monkeypatch.setattr("httpx.request", malformed)
    with pytest.raises(ValueError, match="invalid JSON"):
        client.get_file_contents("group/project", "README.md")

    def missing_content(**kwargs):
        return FakeGitLabResponse(200, {"file_path": "README.md"})

    monkeypatch.setattr("httpx.request", missing_content)
    with pytest.raises(ValueError, match="without content"):
        client.get_file_contents("group/project", "README.md")


def test_openrouter_client_raises_controlled_errors_without_secret_leaks(monkeypatch):
    client = OpenRouterClient(api_key="super-secret-key", model="openai/gpt-4o-mini", max_retries=0)

    def fake_timeout(**kwargs):
        raise httpx.TimeoutException("request timed out")

    monkeypatch.setattr("httpx.request", fake_timeout)
    with pytest.raises(httpx.TimeoutException) as excinfo:
        client.chat_completion([])
    assert "super-secret-key" not in str(excinfo.value)

    def fake_connect(**kwargs):
        raise httpx.ConnectError("dial tcp: bad connection")

    monkeypatch.setattr("httpx.request", fake_connect)
    with pytest.raises(httpx.ConnectError) as excinfo:
        client.chat_completion([])
    assert "super-secret-key" not in str(excinfo.value)

    def fake_error_response(**kwargs):
        return FakeOpenRouterResponse(429, {"error": {"message": "quota exceeded for secret-analyzer"}})

    monkeypatch.setattr("httpx.request", fake_error_response)
    with pytest.raises(OpenRouterAPIError) as excinfo:
        client.chat_completion([])
    assert "secret-analyzer" not in str(excinfo.value)
    assert "status 429" in str(excinfo.value)
    assert "quota" in str(excinfo.value).lower()


def test_openrouter_rejects_empty_invalid_and_incomplete_responses(monkeypatch):
    client = OpenRouterClient(api_key="test-key", model="openai/gpt-4o-mini", max_retries=0)

    def empty_response(**kwargs):
        return FakeOpenRouterResponse(200, {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]})

    monkeypatch.setattr("httpx.request", empty_response)
    with pytest.raises(OpenRouterEmptyContentError):
        client.chat_completion([])

    def invalid_json_response(**kwargs):
        return FakeOpenRouterResponse(200, {"choices": [{"message": {"content": "not-json"}, "finish_reason": "stop"}]})

    monkeypatch.setattr("httpx.request", invalid_json_response)
    with pytest.raises(OpenRouterMalformedJSONError):
        client.chat_completion([])

    def incomplete_response(**kwargs):
        return FakeOpenRouterResponse(200, {"choices": [{"message": {"content": '{"summary": "partial"}'}, "finish_reason": "length"}]})

    monkeypatch.setattr("httpx.request", incomplete_response)
    with pytest.raises(OpenRouterIncompleteResponseError):
        client.chat_completion([])


def test_orchestrator_falls_back_and_respects_global_attempt_budget(monkeypatch, tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("APP_ENV_FILE", str(empty_env))
    monkeypatch.setenv("AI_TOTAL_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("AI_MODEL_COOLDOWN_SECONDS", "300")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test")
    monkeypatch.setenv("GROQ_API_KEY", "groq-test")
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("GROQ_ENABLED", "true")

    MODEL_COOLDOWNS.clear()
    PROVIDER_COOLDOWNS.clear()
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
                raise OpenRouterAPIError("OpenRouter API request failed with status 429: quota exceeded")
            return {"summary": "fallback success"}

    monkeypatch.setattr(orchestrator, "candidates", lambda: candidates)
    monkeypatch.setattr(orchestrator, "_client_for", lambda candidate: FakeClient(candidate.model_id))

    result = orchestrator.chat_completion([])
    assert result == {"summary": "fallback success"}
    assert calls == ["first:free", "second:free"]
    assert orchestrator.attempts == 2
    assert orchestrator.last_failure_reason == "rate limit or quota exhausted"


def test_orchestrator_cooldown_and_quota_breaker_prevent_retry_storm(monkeypatch, tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("APP_ENV_FILE", str(empty_env))
    monkeypatch.setenv("AI_TOTAL_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("AI_MODEL_COOLDOWN_SECONDS", "300")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test")
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    MODEL_COOLDOWNS.clear()
    PROVIDER_COOLDOWNS.clear()
    orchestrator = AIOrchestrator()
    candidates = [AIModelCandidate("openrouter", "quota:free")]

    class FakeClient:
        def __init__(self, model):
            self.model = model

        def chat_completion(self, *args, **kwargs):
            raise OpenRouterAPIError("OpenRouter API request failed with status 429: account-level daily quota reached")

    monkeypatch.setattr(orchestrator, "candidates", lambda: candidates)
    monkeypatch.setattr(orchestrator, "_client_for", lambda candidate: FakeClient(candidate.model_id))

    with pytest.raises(Exception):
        orchestrator.chat_completion([])

    assert "openrouter:quota:free" in MODEL_COOLDOWNS
    assert PROVIDER_COOLDOWNS.get("openrouter") is not None
    assert orchestrator.last_failure_reason == "rate limit or quota exhausted"


def test_model_discovery_filters_audio_and_unusable_results():
    catalog = {
        "data": [
            {"id": "openai/gpt-4o-mini", "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["response_format", "temperature"], "context_length": 128000},
            {"id": "distil-whisper-large-v3", "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["response_format"], "context_length": 32000},
            {"id": "text-embedding-3-small", "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["response_format"], "context_length": 8000},
            {"id": "bad-model", "pricing": {"prompt": "123", "completion": "123"}, "supported_parameters": ["response_format"], "context_length": 16000},
        ]
    }
    results = candidates_from_catalog("groq", catalog, free_only=True)
    ids = [candidate.model_id for candidate in results]
    assert "openai/gpt-4o-mini" in ids
    assert "distil-whisper-large-v3" not in ids
    assert "text-embedding-3-small" not in ids
    assert "bad-model" not in ids
