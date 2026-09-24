import asyncio
from types import SimpleNamespace

import pytest

from app.ai.code_reviewer import normalize_review_result
from app.ai.mr_summary import generate_mr_summary, render_mr_summary
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


def test_review_normalization_keeps_actionable_fields_and_drops_invalid_findings():
    result = normalize_review_result({
        "summary": "Authentication behavior changed.",
        "change_type": "security",
        "risk": "medium",
        "breaking_changes": "potential",
        "findings": [
            {"severity": "high", "category": "security", "file": "app/auth.py", "line": 12, "issue": "Token is logged.", "recommendation": "Remove the log."},
            {"severity": "info", "file": "../../secret.txt", "issue": "unsafe path"},
        ],
    })

    assert result["findings"] == [{
        "severity": "high",
        "category": "security",
        "file": "app/auth.py",
        "line": 12,
        "issue": "Token is logged.",
        "recommendation": "Remove the log.",
        "confidence": "",
    }]
    rendered = render_mr_summary({**result, "deterministic_files": ["app/auth.py (+2 / -1)"]})
    assert "app/auth.py (+2 / -1)" in rendered
    assert "Breaking Changes" in rendered
    assert "No actionable findings identified." not in rendered
    assert "{'severity'" not in rendered


def test_render_summary_has_no_artificial_finding_for_trivial_change():
    rendered = render_mr_summary({
        "summary": "Adds a health import.",
        "change_type": "unknown",
        "risk": "low",
        "findings": [],
        "deterministic_files": ["app/main.py (+1 / -0)"],
    })

    assert "No actionable findings identified." in rendered


def test_gitlab_diff_statistics_are_deterministic_and_preserve_explicit_zeroes():
    class FakeGitLabClient:
        def get_project(self, project_id):
            return {"id": project_id}

        def get_merge_request(self, project_id, mr_iid):
            return {"description": ""}

        def get_merge_request_changes(self, project_id, mr_iid):
            return {"changes": [
                {"new_path": "simple.py", "diff": "@@ -1 +1,3 @@\n line\n+added\n+another"},
                {"new_path": "empty.py", "diff": "", "additions": 0, "deletions": 0},
            ]}

        def get_merge_request_commits(self, project_id, mr_iid):
            return []

        def get_merge_request_approvals(self, project_id, mr_iid):
            return {}

    context = mr_processor.build_mr_context("1", 2, FakeGitLabClient())

    assert context["changed_files"] == [
        {"path": "simple.py", "status": "modified", "additions": 2, "deletions": 0, "old_path": None, "binary": False},
        {"path": "empty.py", "status": "modified", "additions": 0, "deletions": 0, "old_path": None, "binary": False},
    ]


def test_live_output_normalizes_breaking_changes_file_and_related_credentials():
    normalized = normalize_review_result({
        "summary": "Hardcoded credentials are present.",
        "risk": "high",
        "breaking_changes": [],
        "testing": "Run tests after removing hardcoded values.",
        "findings": [
            {"severity": "info", "issue": "Hardcoded AWS_ACCESS_KEY_ID AKIA1234567890123456 and AWS_SECRET_ACCESS_KEY"},
            {"severity": "info", "issue": "Hardcoded GITHUB_TOKEN"},
        ],
    }, changed_files=[{"path": "simple.py", "additions": 2, "deletions": 0}])
    rendered = render_mr_summary({
        **normalized,
        "deterministic_files": ["simple.py (+2 / -0)"],
    })

    assert normalized["breaking_changes"] == "none identified"
    assert len(normalized["findings"]) == 1
    assert normalized["findings"][0]["file"] == "simple.py"
    assert normalized["findings"][0]["category"] == "security"
    assert "Breaking Changes\nnone identified" in rendered
    assert "**info / info**" not in rendered
    assert "`simple.py`" in rendered
    assert "No credential remediation is evidenced" in rendered
    assert "AWS_ACCESS_KEY_ID" in rendered
    assert "AKIA1234567890123456" not in rendered


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
    assert updates[0].count("AI review unavailable:") == 1
    assert "OpenRouter" not in updates[0]
    assert "quota exhausted" not in updates[0]
