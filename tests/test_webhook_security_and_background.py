import time

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
    monkeypatch.setenv("WORKER_DATABASE_PATH", str(tmp_path / "webhook-jobs.sqlite3"))
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


def test_valid_mr_webhook_processes_in_process(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "supersecret")
    called = {"count": 0}

    async def fake_process(*args, **kwargs):
        called["count"] += 1
        return {"status": "accepted"}

    monkeypatch.setattr(mr_processor, "process_gitlab_event", fake_process)
    response = client.post(
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
                "summary": "The change is safe.",
                "change_type": "maintenance",
                "testing": "Not run",
                "risk": "low",
                "files_summary": ["app/main.py"],
                "findings": [{"severity": "low", "file": "app/main.py", "line": 1, "issue": "Example finding"}],
            }

    monkeypatch.setattr(mr_processor, "GitLabClient", FakeGitLabClient)
    monkeypatch.setattr(mr_processor, "AIOrchestrator", FakeAIOrchestrator)
    response = client.post(
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
    first = client.post(
        "/webhook/gitlab",
        json={"object_kind": "merge_request", "object_attributes": {"iid": 1, "action": "open"}, "project": {"id": 9}},
        headers={"X-Gitlab-Token": "supersecret", "webhook-id": "failure-1"},
    )
    assert first.status_code == 200
    for _ in range(25):
        if processed == [1]:
            break
        time.sleep(0.05)

    second = client.post(
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
