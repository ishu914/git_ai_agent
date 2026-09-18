import json

from app.ai.orchestrator import AIOrchestrator, ANALYSIS_CACHE
from app.ai.token_budget import build_compact_review_payload, deduplicate_findings, estimate_tokens


def _context(changes):
    return {
        "merge_request": {
            "title": "Change",
            "description": "Short description",
            "source_branch": "feature",
            "target_branch": "main",
            "sha": "abc123",
        },
        "changed_files": [{"path": item.get("new_path", "unknown")} for item in changes],
        "changes": {"changes": changes},
        "total_additions": 10,
        "total_deletions": 2,
        "total_diff_size": sum(len(item.get("diff", "")) for item in changes),
    }


def test_small_mr_payload_is_compact_and_diff_first():
    payload, partial, excluded = build_compact_review_payload(
        _context([{"new_path": "app.py", "diff": "+return safe_value"}]),
        {"status": "pass", "checks": [{"name": "file_count", "status": "pass"}]},
        1000,
        500,
        5,
    )

    assert partial is False
    assert excluded == []
    assert payload["changes"] == [{"file": "app.py", "diff": "+return safe_value"}]
    assert "repository" not in json.dumps(payload).lower()


def test_generated_and_binary_files_are_excluded():
    payload, partial, excluded = build_compact_review_payload(
        _context([
            {"new_path": "bundle.min.js", "diff": "large generated content"},
            {"new_path": "image.png", "diff": "", "binary": True},
            {"new_path": "app.py", "diff": "+dangerous_call()"},
        ]),
        {"status": "pass", "checks": []},
        1000,
        500,
        5,
    )

    assert [item["file"] for item in payload["changes"]] == ["app.py"]
    assert set(excluded) == {"bundle.min.js", "image.png"}


def test_large_review_reports_partial_scope():
    payload, partial, excluded = build_compact_review_payload(
        _context([{"new_path": f"app_{index}.py", "diff": "x" * 100} for index in range(5)]),
        {"status": "pass", "checks": []},
        150,
        100,
        2,
    )

    assert partial is True
    assert excluded
    assert len(payload["changes"]) <= 2


def test_findings_are_deduplicated_by_root_cause_and_file():
    findings = [
        {"issue": "SQL injection", "file": "app.py"},
        {"issue": "SQL injection", "file": "app.py"},
        {"issue": "SQL injection", "file": "other.py"},
    ]

    assert len(deduplicate_findings(findings)) == 2


def test_estimated_tokens_are_deterministic():
    assert estimate_tokens("12345678") == 2
    assert estimate_tokens({"a": "b"}) == estimate_tokens({"a": "b"})


def test_orchestrator_reuses_cached_analysis(monkeypatch, tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("APP_ENV_FILE", str(empty_env))
    orchestrator = AIOrchestrator()
    calls = {"count": 0}

    def fake_completion(*args, **kwargs):
        calls["count"] += 1
        orchestrator.last_client_usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        orchestrator.last_provider = "test"
        orchestrator.last_model = "test-model"
        return {"summary": "compact", "findings": []}

    monkeypatch.setattr(orchestrator, "chat_completion", fake_completion)
    monkeypatch.setattr(orchestrator.settings, "ai_max_total_tokens_per_mr", 1000)
    context = _context([{"new_path": "app.py", "diff": "+safe()"}])
    validation = {"status": "pass", "checks": []}

    first = orchestrator.analyze_mr("1", 2, context, validation)
    second = orchestrator.analyze_mr("1", 2, context, validation)

    assert first == second
    assert calls["count"] == 1
    ANALYSIS_CACHE.clear()