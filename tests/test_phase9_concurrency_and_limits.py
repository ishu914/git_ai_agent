import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import mr_processor


@pytest.fixture
def phase9_client(monkeypatch, tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    db_path = str(tmp_path / "concurrency_events.sqlite3")

    monkeypatch.setenv("APP_ENV_FILE", str(empty_env))
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("WEBHOOK_MAX_BODY_BYTES", "1000")  # Small limit for testing
    monkeypatch.setenv("GITLAB_TOKEN", "mock-gitlab-token")
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "mock-openrouter-key")
    monkeypatch.delenv("GITLAB_WEBHOOK_SIGNING_TOKEN", raising=False)

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def test_webhook_request_body_size_limit_rejection(phase9_client):
    # Payload > 1000 bytes should be rejected with 413 Payload Too Large
    large_payload = {
        "object_kind": "merge_request",
        "project": {"id": 1},
        "object_attributes": {"iid": 1, "action": "open"},
        "padding": "x" * 2000,
    }
    response = phase9_client.post(
        "/webhook/gitlab",
        json=large_payload,
        headers={"X-Gitlab-Token": "supersecret"},
    )
    assert response.status_code == 413
    assert response.json()["detail"] == "Payload Too Large"


def test_normal_size_webhook_accepted(phase9_client):
    normal_payload = {
        "object_kind": "merge_request",
        "project": {"id": 1},
        "object_attributes": {"iid": 2, "action": "open"},
    }
    response = phase9_client.post(
        "/webhook/gitlab",
        json=normal_payload,
        headers={"X-Gitlab-Token": "supersecret", "webhook-id": "wh-size-normal"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


def test_health_endpoint_fast_and_healthy(phase9_client):
    response = phase9_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "gitlab-ai-agent"
    assert "processing_architecture" in data


def test_ready_endpoint_distinguishes_readiness(phase9_client):
    response = phase9_client.get("/ready")
    assert response.status_code in (200, 503)
    data = response.json()
    assert "event_store" in data
    assert "ai_providers" in data
    assert "available_candidates" in data


def test_metrics_endpoint_exposes_useful_metrics(phase9_client):
    response = phase9_client.get("/metrics")
    assert response.status_code == 200
    metrics = response.json()
    assert "counters" in metrics
    assert "gauges" in metrics
