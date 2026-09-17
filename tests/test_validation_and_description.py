from app.services.description_manager import build_ai_section, replace_ai_section
from app.validation.engine import ValidationEngine


def test_validation_engine_pass_for_small_valid_diff():
    engine = ValidationEngine()
    result = engine.validate({
        "changed_files": [
            {"path": "app/main.py", "status": "modified", "additions": 10, "deletions": 2},
            {"path": "tests/test_main.py", "status": "modified", "additions": 8, "deletions": 1},
        ],
        "total_additions": 18,
        "total_deletions": 3,
        "total_diff_size": 1600,
    })

    assert result["status"] == "pass"
    assert any(check["name"] == "file_count" for check in result["checks"])


def test_validation_engine_rejects_blocked_files():
    engine = ValidationEngine()
    result = engine.validate({
        "changed_files": [
            {"path": ".env", "status": "modified", "additions": 10, "deletions": 0},
        ],
        "total_additions": 10,
        "total_deletions": 0,
        "total_diff_size": 200,
    })

    assert result["status"] == "fail"
    assert any(check["name"] == "blocked_files" for check in result["checks"])


def test_description_manager_replaces_only_ai_marked_section():
    original = "## Developer\n\nText\n\n<!-- AI_REVIEW_START -->\nold\n<!-- AI_REVIEW_END -->\nMore text"
    updated = replace_ai_section(original, build_ai_section({"summary": "new", "risk": "low"}))

    assert "old" not in updated
    assert "new" in updated
    assert "## Developer" in updated
    assert "More text" in updated
