import json
from pathlib import Path

import pytest

from app.ai.evaluation import EvaluationCase, evaluate_case, evaluate_cases
from app.ai.token_budget import build_compact_review_payload


FIXTURES = Path(__file__).parent / "fixtures"


def _cases():
    cases = []
    for fixture in FIXTURES.glob("*.json"):
        for item in json.loads(fixture.read_text(encoding="utf-8")):
            cases.append(EvaluationCase(**item))
    return cases


def test_synthetic_evaluation_corpus_is_present_and_small():
    cases = _cases()

    assert len(cases) >= 13
    assert {case.category for case in cases} >= {"security", "bug", "performance", "safe", "prompt-injection"}
    assert max(len(case.diff) for case in cases) < 1000


def test_evaluation_uses_semantic_invariants_not_exact_response_text():
    case = EvaluationCase(
        case_id="sql",
        category="security",
        file="db.py",
        diff="unsafe query",
        must_detect=("sql injection",),
        acceptable_severities=("high", "critical"),
    )
    report = evaluate_case(
        case,
        {"summary": "Unsafe database access", "findings": [{"severity": "high", "file": "db.py", "issue": "Potential SQL injection", "recommendation": "Use parameters"}]},
    )

    assert report["passed"] is True


def test_safe_case_rejects_false_positive():
    case = EvaluationCase(
        case_id="safe-sql",
        category="safe",
        file="db.py",
        diff="parameterized",
        must_not_detect=("sql injection",),
    )

    report = evaluate_case(case, {"summary": "Parameterized query", "findings": []})

    assert report["passed"] is True


def test_invalid_output_is_normalized_before_evaluation():
    case = EvaluationCase(case_id="safe", category="safe", file="app.py", diff="safe", must_not_detect=("critical",))
    report = evaluate_case(
        case,
        {"summary": "Review", "findings": [{"severity": "critical", "file": "../secret", "issue": "critical"}]},
    )

    assert report["finding_count"] == 0


def test_partial_scope_is_reported_by_payload_builder():
    payload, partial, excluded = build_compact_review_payload(
        {"merge_request": {}, "changed_files": [{"path": "app.py"}], "changes": {"changes": [{"new_path": "app.py", "diff": "x" * 1000}]}},
        {"status": "pass", "checks": []},
        100,
        100,
        1,
    )

    assert partial is True
    assert excluded
    assert payload["changes"]


def test_evaluation_report_metrics_are_transparent():
    cases = [EvaluationCase("one", "safe", "a.py", "", must_detect=("bug",))]
    report = evaluate_cases(cases, {"one": {"summary": "No issue", "findings": []}})

    assert report["metrics"] == {
        "total": 1,
        "passed": 0,
        "failed": 1,
        "false_positive_cases": 0,
        "partial_reviews": 0,
    }
