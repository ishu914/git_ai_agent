from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.services import mr_processor


client = TestClient(app)


@pytest.fixture(autouse=True)
def isolate_webhook_environment(monkeypatch, tmp_path):
    empty_env_file = tmp_path / "empty.env"
    empty_env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("APP_ENV_FILE", str(empty_env_file))
    monkeypatch.delenv("GITLAB_WEBHOOK_SIGNING_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_WEBHOOK_SECRET", raising=False)
    mr_processor.EVENT_STATES.clear()
    yield
    mr_processor.EVENT_STATES.clear()


def test_missing_webhook_token(monkeypatch):
    monkeypatch.delenv("GITLAB_WEBHOOK_SIGNING_TOKEN", raising=False)
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")
    response = client.post(
        "/webhook/gitlab",
        json={"object_kind": "merge_request", "object_attributes": {"iid": 12, "action": "open"}, "project": {"id": 1}},
    )
    assert response.status_code == 401


def test_incorrect_webhook_token(monkeypatch):
    monkeypatch.delenv("GITLAB_WEBHOOK_SIGNING_TOKEN", raising=False)
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")
    response = client.post(
        "/webhook/gitlab",
        json={"object_kind": "merge_request", "object_attributes": {"iid": 12, "action": "open"}, "project": {"id": 1}},
        headers={"X-Gitlab-Token": "wrong-token"},
    )
    assert response.status_code == 401


def test_missing_server_webhook_secret(monkeypatch):
    monkeypatch.delenv("GITLAB_WEBHOOK_SIGNING_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_WEBHOOK_SECRET", raising=False)
    response = client.post(
        "/webhook/gitlab",
        json={"object_kind": "merge_request", "object_attributes": {"iid": 12, "action": "open"}, "project": {"id": 1}},
        headers={"X-Gitlab-Token": "anything"},
    )
    assert response.status_code == 500


def test_malformed_json(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")
    response = client.post(
        "/webhook/gitlab",
        data="{not-json}",
        headers={"X-Gitlab-Token": "supersecret"},
    )
    assert response.status_code == 400


def test_non_mr_event(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")
    response = client.post(
        "/webhook/gitlab",
        json={"object_kind": "push", "project": {"id": 1}},
        headers={"X-Gitlab-Token": "supersecret"},
    )
    assert response.status_code == 400


def test_missing_project_id(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")
    response = client.post(
        "/webhook/gitlab",
        json={"object_kind": "merge_request", "object_attributes": {"iid": 12, "action": "open"}},
        headers={"X-Gitlab-Token": "supersecret"},
    )
    assert response.status_code == 400


def test_missing_mr_iid(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")
    response = client.post(
        "/webhook/gitlab",
        json={"object_kind": "merge_request", "object_attributes": {"action": "open"}, "project": {"id": 1}},
        headers={"X-Gitlab-Token": "supersecret"},
    )
    assert response.status_code == 400


def test_valid_mr_webhook_queues_background(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")
    called = {"count": 0}

    def fake_process(*args, **kwargs):
        called["count"] += 1

    monkeypatch.setattr(mr_processor, "process_gitlab_event", fake_process)
    response = client.post(
        "/webhook/gitlab",
        json={"object_kind": "merge_request", "object_attributes": {"iid": 12, "action": "open"}, "project": {"id": 1}},
        headers={"X-Gitlab-Token": "supersecret", "X-Gitlab-Event": "Merge Request Hook"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert called["count"] == 0


def test_duplicate_event_not_queued_twice(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")
    event_key = "1:12:open"
    mr_processor.EVENT_STATES[event_key] = "pending"
    response = client.post(
        "/webhook/gitlab",
        json={"object_kind": "merge_request", "object_attributes": {"iid": 12, "action": "open"}, "project": {"id": 1}},
        headers={"X-Gitlab-Token": "supersecret", "X-Gitlab-Event": "Merge Request Hook"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
    mr_processor.EVENT_STATES.pop(event_key, None)


def test_background_failure_is_logged_and_does_not_crash_app(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")

    async def boom(*args, **kwargs):
        raise RuntimeError("background failure")

    monkeypatch.setattr(mr_processor, "process_gitlab_event", boom)
    response = client.post(
        "/webhook/gitlab",
        json={"object_kind": "merge_request", "object_attributes": {"iid": 12, "action": "open"}, "project": {"id": 1}},
        headers={"X-Gitlab-Token": "supersecret", "X-Gitlab-Event": "Merge Request Hook"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
