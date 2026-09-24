import time
import re
import asyncio
import threading

from fastapi.testclient import TestClient
import pytest

from app.main import app, create_app
from app.events.manager import set_event_manager
from app.services import mr_processor
from app.services.description_manager import sanitize_gitlab_ai_text


client = TestClient(app)


@pytest.fixture(autouse=True)
def isolate_webhook_environment(monkeypatch, tmp_path):
    empty_env_file = tmp_path / "empty.env"
    empty_env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("APP_ENV_FILE", str(empty_env_file))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "webhook-jobs.sqlite3"))
    monkeypatch.delenv("GITLAB_WEBHOOK_SIGNING_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("GITLAB_TOKEN", "test-gitlab-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    set_event_manager(None)
    yield
    set_event_manager(None)


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


def test_valid_mr_webhook_processes_once_through_dispatcher(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")
    called = {"count": 0}

    async def fake_process(*args, **kwargs):
        called["count"] += 1
        return {"status": "accepted"}

    monkeypatch.setattr(mr_processor, "process_gitlab_event", fake_process)
    with TestClient(create_app()) as running_client:
        response = running_client.post(
            "/webhook/gitlab",
            json={"object_kind": "merge_request", "object_attributes": {"iid": 12, "action": "open"}, "project": {"id": 1}},
            headers={"X-Gitlab-Token": "supersecret", "X-Gitlab-Event": "Merge Request Hook"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "accepted"
        for _ in range(25):
            if called["count"] >= 1:
                break
            time.sleep(0.05)
    assert called["count"] == 1


def test_duplicate_event_not_queued_twice(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")
    payload = {"object_kind": "merge_request", "object_attributes": {"iid": 12, "action": "open"}, "project": {"id": 1}}
    headers = {"X-Gitlab-Token": "supersecret", "X-Gitlab-Event": "Merge Request Hook"}
    first = client.post(
        "/webhook/gitlab",
        json=payload, headers=headers,
    )
    second = client.post("/webhook/gitlab", json=payload, headers=headers)
    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"


def test_webhook_rejects_new_delivery_when_durable_queue_is_full(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")
    monkeypatch.setenv("EVENT_MAX_QUEUE_DEPTH", "1")
    headers = {"X-Gitlab-Token": "supersecret"}
    first = {"object_kind": "merge_request", "project": {"id": 1}, "object_attributes": {"iid": 1, "action": "open"}}
    second = {"object_kind": "merge_request", "project": {"id": 1}, "object_attributes": {"iid": 2, "action": "open"}}
    assert client.post("/webhook/gitlab", json=first, headers=headers).json()["status"] == "accepted"
    response = client.post("/webhook/gitlab", json=second, headers=headers)
    assert response.status_code == 429
    assert response.json()["detail"] == "Event queue is at capacity"


def test_payload_hash_deduplicates_retries_but_accepts_distinct_updates(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")
    processed = []

    async def fake_process(event):
        processed.append(event["object_attributes"]["updated_at"])
        return {}

    monkeypatch.setattr(mr_processor, "process_gitlab_event", fake_process)
    headers = {"X-Gitlab-Token": "supersecret"}
    first = {"object_kind": "merge_request", "project": {"id": 3}, "changes": {"diffs": {"old": "base"}}, "object_attributes": {"iid": 4, "action": "update", "updated_at": "2026-01-01T00:00:00Z"}}
    second = {"object_kind": "merge_request", "project": {"id": 3}, "changes": {"diffs": {"new": "changed"}}, "object_attributes": {"iid": 4, "action": "update", "updated_at": "2026-01-01T00:01:00Z"}}
    with TestClient(create_app()) as running_client:
        assert running_client.post("/webhook/gitlab", json=first, headers=headers).json()["status"] == "accepted"
        assert running_client.post("/webhook/gitlab", json=second, headers=headers).json()["status"] == "accepted"
        assert running_client.post("/webhook/gitlab", json=first, headers=headers).json()["status"] == "duplicate"
        for _ in range(25):
            if len(processed) == 2:
                break
            time.sleep(0.05)
    assert sorted(processed) == ["2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"]


def test_ai_gitlab_writes_neutralize_quick_actions():
    malicious = "/approve\n/merge\n/close\n/assign @user\nordinary /merge text"
    sanitized = sanitize_gitlab_ai_text(malicious)
    assert not re.search(r"^\s*/(?:approve|merge|close|assign)\b", sanitized, flags=re.MULTILINE)
    assert "ordinary /merge text" in sanitized

    writes = []

    class FakeClient:
        def get_merge_request_notes(self, *_):
            return []

        def create_merge_request_note(self, _, __, body):
            writes.append(body)

    mr_processor._publish_ai_review(
        FakeClient(), "project", 1,
        {"status": "reviewed", "summary": malicious, "findings": []},
    )
    # The same central function protects review notes and description sections.
    writes.append(sanitize_gitlab_ai_text(malicious))
    assert all(not re.search(r"^\s*/(?:approve|merge|close|assign)\b", body, flags=re.MULTILINE) for body in writes)


def test_health_remains_responsive_while_dispatcher_processes_slow_event(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")
    started = threading.Event()
    release = threading.Event()

    async def slow_process(_event):
        started.set()
        await asyncio.to_thread(release.wait, 3)
        return {}

    monkeypatch.setattr(mr_processor, "process_gitlab_event", slow_process)
    with TestClient(create_app()) as running_client:
        response = running_client.post(
            "/webhook/gitlab",
            json={"object_kind": "merge_request", "project": {"id": 8}, "object_attributes": {"iid": 9, "action": "open"}},
            headers={"X-Gitlab-Token": "supersecret"},
        )
        assert response.status_code == 200
        assert started.wait(1), "slow processing did not start"
        started_at = time.monotonic()
        health = running_client.get("/health")
        elapsed = time.monotonic() - started_at
        release.set()
    assert health.status_code == 200
    assert elapsed < 1


def test_background_failure_is_logged_and_does_not_crash_app(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")

    async def boom(*args, **kwargs):
        raise RuntimeError("background failure")

    monkeypatch.setattr(mr_processor, "process_gitlab_event", boom)
    with TestClient(create_app()) as running_client:
        response = running_client.post(
        "/webhook/gitlab",
        json={"object_kind": "merge_request", "object_attributes": {"iid": 12, "action": "open"}, "project": {"id": 1}},
        headers={"X-Gitlab-Token": "supersecret", "X-Gitlab-Event": "Merge Request Hook"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


def test_single_process_e2e_updates_description_and_review_note(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")
    monkeypatch.setenv("GITLAB_TOKEN", "gitlab-token")
    calls = []

    class FakeGitLabClient:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs))

        def get_project(self, project_id):
            calls.append(("get_project", project_id))
            return {"id": project_id, "path_with_namespace": "group/project"}

        def get_merge_request(self, project_id, mr_iid):
            calls.append(("get_merge_request", project_id, mr_iid))
            return {
                "description": "Developer-authored text\n\n<!-- AI_REVIEW_START -->\nold\n<!-- AI_REVIEW_END -->",
                "title": "Safe change",
                "sha": "abc123",
            }

        def get_merge_request_changes(self, project_id, mr_iid):
            calls.append(("get_merge_request_changes", project_id, mr_iid))
            return {"changes": [{"new_path": "app/main.py", "diff": "+return 1", "additions": 1, "deletions": 0}]}

        def get_merge_request_commits(self, project_id, mr_iid):
            calls.append(("get_merge_request_commits", project_id, mr_iid))
            return []

        def get_merge_request_approvals(self, project_id, mr_iid):
            calls.append(("get_merge_request_approvals", project_id, mr_iid))
            return {}

        def update_merge_request_description(self, project_id, mr_iid, description):
            calls.append(("update_description", project_id, mr_iid, description))
            return {}

        def get_merge_request_notes(self, project_id, mr_iid):
            calls.append(("get_notes", project_id, mr_iid))
            return []

        def create_merge_request_note(self, project_id, mr_iid, body):
            calls.append(("create_note", project_id, mr_iid, body))
            return {"id": 1, "body": body}

    class FakeAIOrchestrator:
        last_failure_reason = ""

        def analyze_mr(self, project_id, mr_iid, context, validation):
            calls.append(("ai_analyze", project_id, mr_iid, validation["status"]))
            return {
                "status": "reviewed",
                "summary": "The change is safe.\n/approve\n/merge\n/close\n/assign @user",
                "change_type": "maintenance",
                "testing": "Not run",
                "risk": "low",
                "files_summary": ["app/main.py"],
                "findings": [{"severity": "low", "file": "app/main.py", "line": 1, "issue": "Example finding"}],
            }

    monkeypatch.setattr(mr_processor, "GitLabClient", FakeGitLabClient)
    monkeypatch.setattr(mr_processor, "AIOrchestrator", FakeAIOrchestrator)
    with TestClient(create_app()) as running_client:
        response = running_client.post(
            "/webhook/gitlab",
            json={"object_kind": "merge_request", "object_attributes": {"iid": 42, "action": "open"}, "project": {"id": 7}},
            headers={"X-Gitlab-Token": "supersecret", "X-Gitlab-Event": "Merge Request Hook", "webhook-id": "e2e-42"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "accepted"
        for _ in range(25):
            if any(call[0] == "create_note" for call in calls):
                break
            time.sleep(0.05)

    updated = next(call[3] for call in calls if call[0] == "update_description")
    note = next(call[3] for call in calls if call[0] == "create_note")
    assert "Developer-authored text" in updated
    assert "The change is safe." in updated
    assert "AI_REVIEW_NOTE" in note
    assert not re.search(r"^\s*/(?:approve|merge|close|assign)\b", updated, flags=re.MULTILINE)
    assert not re.search(r"^\s*/(?:approve|merge|close|assign)\b", note, flags=re.MULTILINE)
    assert any(call[0] == "ai_analyze" for call in calls)
    assert not any(call[0] in {"approve", "merge", "push", "update_branch", "change_permissions"} for call in calls)


def test_failed_in_process_task_does_not_block_another_webhook(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")
    processed = []

    async def process(event):
        iid = event["object_attributes"]["iid"]
        processed.append(iid)
        if iid == 1:
            raise RuntimeError("expected isolated failure")

    monkeypatch.setattr(mr_processor, "process_gitlab_event", process)
    with TestClient(create_app()) as running_client:
        first = running_client.post(
            "/webhook/gitlab",
            json={"object_kind": "merge_request", "object_attributes": {"iid": 1, "action": "open"}, "project": {"id": 9}},
            headers={"X-Gitlab-Token": "supersecret", "webhook-id": "failure-1"},
        )
        assert first.status_code == 200
        for _ in range(25):
            if processed == [1]:
                break
            time.sleep(0.05)

        second = running_client.post(
            "/webhook/gitlab",
            json={"object_kind": "merge_request", "object_attributes": {"iid": 2, "action": "open"}, "project": {"id": 9}},
            headers={"X-Gitlab-Token": "supersecret", "webhook-id": "success-2"},
        )
        assert second.status_code == 200
        for _ in range(25):
            if processed == [1, 2]:
                break
            time.sleep(0.05)
    assert processed == [1, 2]
