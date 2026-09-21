from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from app.ai.code_reviewer import normalize_review_result


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    category: str
    file: str
    diff: str
    must_detect: tuple[str, ...] = ()
    must_not_detect: tuple[str, ...] = ()
    acceptable_severities: tuple[str, ...] = ("critical", "high", "medium", "low", "info", "warning")


def _finding_text(finding: Dict[str, Any]) -> str:
    return " ".join(str(finding.get(key) or "") for key in ("issue", "recommendation", "file")).lower()


def evaluate_case(case: EvaluationCase, result: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_review_result(result)
    findings = normalized["findings"]
    finding_text = " ".join(_finding_text(finding) for finding in findings)
    missing = [concept for concept in case.must_detect if concept.lower() not in finding_text]
    prohibited = [concept for concept in case.must_not_detect if concept.lower() in finding_text]
    invalid_severity = [
        finding.get("severity")
        for finding in findings
        if finding.get("severity") not in case.acceptable_severities
    ]
    invalid_files = [finding.get("file") for finding in findings if finding.get("file") != case.file]
    passed = not missing and not prohibited and not invalid_severity and not invalid_files
    return {
        "case_id": case.case_id,
        "category": case.category,
        "passed": passed,
        "missing": missing,
        "prohibited": prohibited,
        "invalid_severity": invalid_severity,
        "irrelevant_files": invalid_files,
        "finding_count": len(findings),
        "review_scope": normalized.get("review_scope", "full"),
    }


def evaluate_cases(cases: Iterable[EvaluationCase], results: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    reports = [evaluate_case(case, results.get(case.case_id, {})) for case in cases]
    return {
        "cases": reports,
        "metrics": {
            "total": len(reports),
            "passed": sum(report["passed"] for report in reports),
            "failed": sum(not report["passed"] for report in reports),
            "false_positive_cases": sum(bool(report["prohibited"]) for report in reports),
            "partial_reviews": sum(report["review_scope"] == "partial" for report in reports),
        },
    }
