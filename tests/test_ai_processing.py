import asyncio
from types import SimpleNamespace

import pytest

from app.ai.mr_summary import generate_mr_summary
from app.ai.types import AIUnavailableError
from app.services import mr_processor
from app.services.description_manager import build_ai_section


class FakeAIClient:
    def __init__(self, result):
        self.result = result

    def chat_completion(self, **kwargs):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _context(description="Developer description"):
    return {
        "project": {"id": 4, "path_with_namespace": "group/project"},
        "merge_request": {
            "title": "Small change",
            "description": description,
            "source_branch": "feature",
            "target_branch": "main",
        },
        "changes": {"changes": [{"new_path": "hello.py", "diff": "+print('hi')"}]},
    }


def test_summary_normalizes_string_change_type():
    result = generate_mr_summary(
        FakeAIClient({
            "summary": "Adds a greeting.",
            "change_type": "feature",
            "files_summary": "hello.py",
            "testing": "Not run",
            "risk": "low",
        }),
        _context(),
    )

    assert "### Change Type\nfeature" in result
    assert "f, e, a, t, u, r, e" not in result


def test_summary_normalizes_list_change_type():
    result = generate_mr_summary(
        FakeAIClient({"summary": "Updates behavior.", "change_type": ["feature", "test"]}),
        _context(),
    )

    assert "### Change Type\nfeature, test" in result


def test_missing_summary_returns_unavailable_empty_result():
    result = generate_mr_summary(FakeAIClient({"change_type": "feature"}), _context())

    assert result == ""


def test_description_section_preserves_developer_content():
    original = "Developer content\n\n" + build_ai_section({"summary": "old"}) + "\n\nMore developer content"
    replacement = build_ai_section({"summary": "new"})

    updated = mr_processor.replace_ai_section(original, replacement)

    assert "Developer content" in updated
    assert "More developer content" in updated
    assert "old" not in updated
    assert updated.count("<!-- AI_REVIEW_START -->") == 1


def test_publish_ai_review_creates_note():
    client = SimpleNamespace(
        get_merge_request_notes=lambda project_id, mr_iid: [],
        create_merge_request_note=lambda project_id, mr_iid, body: {"id": 1, "body": body},
        update_merge_request_note=lambda *args: pytest.fail("unexpected note update"),
    )

    mr_processor._publish_ai_review(client, "4", 2, {"status": "reviewed", "summary": "Looks good", "findings": []})


def test_publish_ai_review_updates_existing_note():
    calls = []
    client = SimpleNamespace(
        get_merge_request_notes=lambda project_id, mr_iid: [{"id": 9, "body": "<!-- AI_REVIEW_NOTE -->\nold"}],
        create_merge_request_note=lambda *args: pytest.fail("unexpected note creation"),
        update_merge_request_note=lambda *args: calls.append(args),
    )

    mr_processor._publish_ai_review(client, "4", 2, {"status": "reviewed", "summary": "Updated", "findings": []})

    assert calls[0][0:3] == ("4", 2, 9)
    assert "Updated" in calls[0][3]


def test_unavailable_review_does_not_create_note():
    client = SimpleNamespace(
        get_merge_request_notes=lambda *args: pytest.fail("notes should not be queried"),
        create_merge_request_note=lambda *args: pytest.fail("unavailable review must not create a note"),
    )

    mr_processor._publish_ai_review(client, "4", 2, {"status": "unavailable", "summary": "AI review could not be completed.", "findings": []})


def test_processor_updates_unavailable_status_after_all_ai_failures(monkeypatch):
    updates = []

    class FakeGitLabClient:
        def __init__(self, **kwargs):
            pass

        def get_project(self, project_id):
            return {"id": project_id, "path_with_namespace": "group/project"}

        def get_merge_request(self, project_id, mr_iid):
            return {"description": "Developer content"}

        def get_merge_request_changes(self, project_id, mr_iid):
            return {"changes": [{"new_path": "hello.py", "diff": "+print('hi')"}]}

        def get_merge_request_commits(self, project_id, mr_iid):
            return []

        def get_merge_request_approvals(self, project_id, mr_iid):
            return {}

        def update_merge_request_description(self, project_id, mr_iid, description):
            updates.append(description)
            return {}

        def get_merge_request_notes(self, project_id, mr_iid):
            pytest.fail("unavailable AI must not query or create a review note")

    class UnavailableOrchestrator:
        last_failure_reason = "OpenRouter free-model quota exhausted."

        def chat_completion(self, *args, **kwargs):
            raise AIUnavailableError("all providers failed")

    monkeypatch.setattr(mr_processor, "GitLabClient", FakeGitLabClient)
    monkeypatch.setattr(mr_processor, "AIOrchestrator", UnavailableOrchestrator)
    monkeypatch.setattr(
        mr_processor,
        "get_settings",
        lambda: SimpleNamespace(gitlab_url="http://gitlab", gitlab_token="gitlab-token"),
    )

    result = asyncio.run(
        mr_processor.process_gitlab_event({
            "project": {"id": 4},
            "object_attributes": {"iid": 2, "action": "open"},
        })
    )

    assert result["validation"]["status"] in {"pass", "warn", "fail"}
    assert result["summary_status"] == "unavailable"
    assert "AI Status\nUnavailable" in updates[0]
    assert "Developer content" in updates[0]