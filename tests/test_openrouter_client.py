import os

import pytest

from app.ai.client import OpenRouterClient


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


def test_client_raises_for_missing_api_key():
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterClient(api_key=None)
