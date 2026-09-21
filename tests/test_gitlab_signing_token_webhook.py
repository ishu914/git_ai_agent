import base64
import hashlib
import hmac
import time

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.services import mr_processor

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolate_webhook_environment(monkeypatch, tmp_path):
    empty_env_file = tmp_path / "empty.env"
    empty_env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("APP_ENV_FILE", str(empty_env_file))
    monkeypatch.setenv("WORKER_DATABASE_PATH", str(tmp_path / "webhook-jobs.sqlite3"))
    monkeypatch.delenv("GITLAB_WEBHOOK_SIGNING_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_WEBHOOK_SECRET", raising=False)


def _make_signature(signing_token: str, webhook_id: str, timestamp: str, raw_body: bytes) -> str:
    if not signing_token.startswith("whsec_"):
        raise ValueError("Signing token must start with whsec_")
    key = base64.b64decode(signing_token[6:])
    payload = f"{webhook_id}.{timestamp}.".encode("utf-8") + raw_body
    digest = hmac.new(key, payload, hashlib.sha256).digest()
    return f"v1,{base64.b64encode(digest).decode('ascii')}"


def test_gitlab_signing_token_loaded_from_config(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", "whsec_" + base64.b64encode(b"secret-key").decode("ascii"))
    monkeypatch.delenv("GITLAB_WEBHOOK_SIGNING_TOKEN", raising=False)
    settings = get_settings()
    assert hasattr(settings, "gitlab_webhook_signing_token")


def test_gitlab_webhook_timestamp_tolerance_default(monkeypatch):
    monkeypatch.delenv("GITLAB_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS", raising=False)
    settings = get_settings()
    assert settings.gitlab_webhook_timestamp_tolerance_seconds == 300


def test_valid_signing_token_and_valid_signature(monkeypatch):
    signing_token = "whsec_" + base64.b64encode(b"secret-key").decode("ascii")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", signing_token)

    raw_body = b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"open"},"project":{"id":1}}'
    webhook_id = "evt_123"
    timestamp = str(int(time.time()))
    signature = _make_signature(signing_token, webhook_id, timestamp, raw_body)

    response = client.post(
        "/webhook/gitlab",
        content=raw_body,
        headers={
            "webhook-id": webhook_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": signature,
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


def test_missing_webhook_id(monkeypatch):
    signing_token = "whsec_" + base64.b64encode(b"secret-key").decode("ascii")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", signing_token)
    raw_body = b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"open"},"project":{"id":1}}'
    response = client.post(
        "/webhook/gitlab",
        content=raw_body,
        headers={
            "webhook-timestamp": str(int(time.time())),
            "webhook-signature": "v1,deadbeef",
        },
    )
    assert response.status_code == 401


def test_missing_webhook_timestamp(monkeypatch):
    signing_token = "whsec_" + base64.b64encode(b"secret-key").decode("ascii")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", signing_token)
    raw_body = b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"open"},"project":{"id":1}}'
    response = client.post(
        "/webhook/gitlab",
        content=raw_body,
        headers={
            "webhook-id": "evt_123",
            "webhook-signature": "v1,deadbeef",
        },
    )
    assert response.status_code == 401


def test_missing_webhook_signature(monkeypatch):
    signing_token = "whsec_" + base64.b64encode(b"secret-key").decode("ascii")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", signing_token)
    raw_body = b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"open"},"project":{"id":1}}'
    response = client.post(
        "/webhook/gitlab",
        content=raw_body,
        headers={
            "webhook-id": "evt_123",
            "webhook-timestamp": str(int(time.time())),
        },
    )
    assert response.status_code == 401


def test_invalid_signature(monkeypatch):
    signing_token = "whsec_" + base64.b64encode(b"secret-key").decode("ascii")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", signing_token)
    raw_body = b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"open"},"project":{"id":1}}'
    response = client.post(
        "/webhook/gitlab",
        content=raw_body,
        headers={
            "webhook-id": "evt_123",
            "webhook-timestamp": str(int(time.time())),
            "webhook-signature": "v1,notvalid",
        },
    )
    assert response.status_code == 401


def test_modified_request_body(monkeypatch):
    signing_token = "whsec_" + base64.b64encode(b"secret-key").decode("ascii")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", signing_token)
    raw_body = b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"open"},"project":{"id":1}}'
    webhook_id = "evt_123"
    timestamp = str(int(time.time()))
    signature = _make_signature(signing_token, webhook_id, timestamp, raw_body)

    response = client.post(
        "/webhook/gitlab",
        content=b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"updated"},"project":{"id":1}}',
        headers={
            "webhook-id": webhook_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": signature,
        },
    )
    assert response.status_code == 401


def test_modified_webhook_id(monkeypatch):
    signing_token = "whsec_" + base64.b64encode(b"secret-key").decode("ascii")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", signing_token)
    raw_body = b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"open"},"project":{"id":1}}'
    timestamp = str(int(time.time()))
    valid_signature = _make_signature(signing_token, "evt_123", timestamp, raw_body)

    response = client.post(
        "/webhook/gitlab",
        content=raw_body,
        headers={
            "webhook-id": "evt_456",
            "webhook-timestamp": timestamp,
            "webhook-signature": valid_signature,
        },
    )
    assert response.status_code == 401


def test_modified_timestamp(monkeypatch):
    signing_token = "whsec_" + base64.b64encode(b"secret-key").decode("ascii")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", signing_token)
    raw_body = b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"open"},"project":{"id":1}}'
    webhook_id = "evt_123"
    timestamp = str(int(time.time()))
    valid_signature = _make_signature(signing_token, webhook_id, timestamp, raw_body)

    response = client.post(
        "/webhook/gitlab",
        content=raw_body,
        headers={
            "webhook-id": webhook_id,
            "webhook-timestamp": str(int(timestamp) + 10),
            "webhook-signature": valid_signature,
        },
    )
    assert response.status_code == 401


def test_expired_timestamp(monkeypatch):
    signing_token = "whsec_" + base64.b64encode(b"secret-key").decode("ascii")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", signing_token)
    raw_body = b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"open"},"project":{"id":1}}'
    webhook_id = "evt_123"
    expired_timestamp = str(int(time.time()) - 600)
    valid_signature = _make_signature(signing_token, webhook_id, expired_timestamp, raw_body)

    response = client.post(
        "/webhook/gitlab",
        content=raw_body,
        headers={
            "webhook-id": webhook_id,
            "webhook-timestamp": expired_timestamp,
            "webhook-signature": valid_signature,
        },
    )
    assert response.status_code == 401


def test_future_timestamp_outside_tolerance(monkeypatch):
    signing_token = "whsec_" + base64.b64encode(b"secret-key").decode("ascii")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", signing_token)
    raw_body = b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"open"},"project":{"id":1}}'
    webhook_id = "evt_123"
    future_timestamp = str(int(time.time()) + 600)
    valid_signature = _make_signature(signing_token, webhook_id, future_timestamp, raw_body)

    response = client.post(
        "/webhook/gitlab",
        content=raw_body,
        headers={
            "webhook-id": webhook_id,
            "webhook-timestamp": future_timestamp,
            "webhook-signature": valid_signature,
        },
    )
    assert response.status_code == 401


def test_invalid_timestamp_format(monkeypatch):
    signing_token = "whsec_" + base64.b64encode(b"secret-key").decode("ascii")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", signing_token)
    raw_body = b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"open"},"project":{"id":1}}'
    response = client.post(
        "/webhook/gitlab",
        content=raw_body,
        headers={
            "webhook-id": "evt_123",
            "webhook-timestamp": "abc",
            "webhook-signature": "v1,deadbeef",
        },
    )
    assert response.status_code == 401


def test_invalid_signing_token_base64(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", "whsec_not-valid-base64")
    raw_body = b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"open"},"project":{"id":1}}'
    response = client.post(
        "/webhook/gitlab",
        content=raw_body,
        headers={
            "webhook-id": "evt_123",
            "webhook-timestamp": str(int(time.time())),
            "webhook-signature": "v1,deadbeef",
        },
    )
    assert response.status_code == 401


def test_multiple_valid_signatures_accepts_any_one(monkeypatch):
    signing_token = "whsec_" + base64.b64encode(b"secret-key").decode("ascii")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", signing_token)
    raw_body = b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"open"},"project":{"id":1}}'
    webhook_id = "evt_123"
    timestamp = str(int(time.time()))
    valid_signature = _make_signature(signing_token, webhook_id, timestamp, raw_body)
    response = client.post(
        "/webhook/gitlab",
        content=raw_body,
        headers={
            "webhook-id": webhook_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": f"v1,wrong {valid_signature}",
        },
    )
    assert response.status_code == 200


def test_unsupported_signature_version(monkeypatch):
    signing_token = "whsec_" + base64.b64encode(b"secret-key").decode("ascii")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", signing_token)
    raw_body = b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"open"},"project":{"id":1}}'
    response = client.post(
        "/webhook/gitlab",
        content=raw_body,
        headers={
            "webhook-id": "evt_123",
            "webhook-timestamp": str(int(time.time())),
            "webhook-signature": "v2,deadbeef",
        },
    )
    assert response.status_code == 401


def test_duplicate_webhook_id_rejected(monkeypatch):
    signing_token = "whsec_" + base64.b64encode(b"secret-key").decode("ascii")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", signing_token)
    raw_body = b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"open"},"project":{"id":1}}'
    webhook_id = "evt_duplicate"
    timestamp = str(int(time.time()))
    signature = _make_signature(signing_token, webhook_id, timestamp, raw_body)

    mr_processor.EVENT_STATES[webhook_id] = "pending"
    try:
        response = client.post(
            "/webhook/gitlab",
            content=raw_body,
            headers={
                "webhook-id": webhook_id,
                "webhook-timestamp": timestamp,
                "webhook-signature": signature,
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "duplicate"
    finally:
        mr_processor.EVENT_STATES.pop(webhook_id, None)


def test_background_failure_after_valid_signature_is_logged(monkeypatch):
    signing_token = "whsec_" + base64.b64encode(b"secret-key").decode("ascii")
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", signing_token)
    raw_body = b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"open"},"project":{"id":1}}'
    webhook_id = "evt_fail"
    timestamp = str(int(time.time()))
    signature = _make_signature(signing_token, webhook_id, timestamp, raw_body)

    async def boom(*args, **kwargs):
        raise RuntimeError("background failure")

    monkeypatch.setattr(mr_processor, "process_gitlab_event", boom)

    response = client.post(
        "/webhook/gitlab",
        content=raw_body,
        headers={
            "webhook-id": webhook_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": signature,
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


def test_legacy_x_gitlab_token_fallback(monkeypatch):
    monkeypatch.delenv("GITLAB_WEBHOOK_SIGNING_TOKEN", raising=False)
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "legacy-secret")
    raw_body = b'{"object_kind":"merge_request","object_attributes":{"iid":12,"action":"open"},"project":{"id":1}}'
    response = client.post(
        "/webhook/gitlab",
        content=raw_body,
        headers={"X-Gitlab-Token": "legacy-secret"},
    )
    assert response.status_code == 200


def test_ai_user_webhook_is_skipped_before_background_processing(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "legacy-secret")

    def fail_process(*args, **kwargs):
        pytest.fail("AI-generated webhook must not be queued")

    monkeypatch.setattr(mr_processor, "process_gitlab_event", fail_process)
    response = client.post(
        "/webhook/gitlab",
        json={
            "object_kind": "merge_request",
            "user": {"username": "gi_ai_code_reviewer"},
            "object_attributes": {"iid": 12, "action": "update"},
            "project": {"id": 1},
        },
        headers={"X-Gitlab-Token": "legacy-secret"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "skipped"
    assert response.json()["reason"] == "ai_reviewer_event"


def test_completed_webhook_id_is_not_processed_again(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "legacy-secret")
    mr_processor.EVENT_STATES["evt_completed"] = "completed"
    response = client.post(
        "/webhook/gitlab",
        json={
            "object_kind": "merge_request",
            "object_attributes": {"iid": 12, "action": "open"},
            "project": {"id": 1},
        },
        headers={"X-Gitlab-Token": "legacy-secret", "X-Gitlab-Event-UUID": "evt_completed"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
