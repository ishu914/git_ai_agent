import logging
import re
from typing import Any, Dict
from urllib.parse import unquote

from app.ai.client import OpenRouterClient
from app.ai.prompts import build_messages_for_review
from app.ai.token_budget import deduplicate_findings

logger = logging.getLogger(__name__)
ALLOWED_STATUSES = {"reviewed", "unavailable", "pass", "warn", "fail"}
ALLOWED_SEVERITIES = {"critical", "high", "medium", "low", "info", "warning"}
ALLOWED_CHANGE_TYPES = {"feature", "bugfix", "refactor", "performance", "security", "documentation", "testing", "configuration", "dependency", "chore", "mixed", "unknown"}
ALLOWED_RISKS = {"low", "medium", "high", "unknown"}
ALLOWED_BREAKING = {"none identified", "potential", "confirmed", "unknown"}


def _safe_finding(value: Any) -> Dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    issue = value.get("issue") or value.get("message") or value.get("description")
    file_name = value.get("file") or value.get("path")
    if not isinstance(issue, str) or not issue.strip() or not isinstance(file_name, str) or not file_name.strip():
        return None
    normalized_file = unquote(file_name.strip()).replace("\\", "/")
    if not normalized_file or normalized_file in {".", ".."}:
        return None
    if "\x00" in normalized_file or len(normalized_file) > 300:
        return None
    if normalized_file.startswith("/") or normalized_file.startswith("\\") or re.match(r"^[A-Za-z]:/", normalized_file):
        return None
    segments = [segment for segment in normalized_file.split("/") if segment not in ("", ".")]
    if not segments or any(segment == ".." for segment in segments):
        return None
    normalized_file = "/".join(segments)
    severity = str(value.get("severity") or "info").lower().strip()
    if severity not in ALLOWED_SEVERITIES:
        severity = "info"
    line = value.get("line")
    if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
        line = None
    return {
        "severity": severity,
        "category": str(value.get("category") or "info").lower().strip()[:40],
        "file": normalized_file[:300],
        "line": line,
        "issue": issue.strip()[:1000],
        "recommendation": str(value.get("recommendation") or "").strip()[:1000],
        "confidence": str(value.get("confidence") or "").lower().strip()[:20],
    }


def normalize_review_result(result: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(result, dict) or not result.get("summary"):
        return {"status": "unavailable", "summary": "AI review could not be completed.", "findings": []}
    findings = result.get("findings", [])
    if isinstance(findings, dict):
        findings = [findings]
    if not isinstance(findings, list):
        findings = []
    findings = [finding for finding in (_safe_finding(item) for item in findings) if finding is not None]
    findings = deduplicate_findings(findings)
    status = str(result.get("status") or "reviewed").lower().strip()
    if status not in ALLOWED_STATUSES:
        status = "reviewed"
    change_type = str(result.get("change_type") or "unknown").lower().strip()
    risk = str(result.get("risk") or "unknown").lower().strip()
    breaking_changes = str(result.get("breaking_changes") or "unknown").lower().strip()
    return {
        "status": status,
        "summary": str(result["summary"]).strip(),
        "findings": findings,
        "review_scope": result.get("review_scope", "full"),
        "excluded_files": result.get("excluded_files", []),
        "breaking_changes": breaking_changes if breaking_changes in ALLOWED_BREAKING else "unknown",
        "reviewer_attention": result.get("reviewer_attention", []),
        "testing": result.get("testing", ""),
        "risk": risk if risk in ALLOWED_RISKS else "unknown",
        "change_type": change_type if change_type in ALLOWED_CHANGE_TYPES else "unknown",
    }


def review_merge_request(ai_client: OpenRouterClient, mr_context: Dict[str, Any]) -> Dict[str, Any]:
    mr = mr_context.get("merge_request", {})
    changes = mr_context.get("changes", {}).get("changes", [])
    diff_preview = "\n".join((entry.get("diff") or "")[:1500] for entry in changes[:20])

    prompt = {
        "title": mr.get("title", ""),
        "source_branch": mr.get("source_branch", ""),
        "target_branch": mr.get("target_branch", ""),
        "diff": diff_preview[:6000],
        "instruction": "Return compact JSON: status, summary, findings. Findings must be a short list of concrete issues with severity and file.",
    }

    messages = build_messages_for_review("code-review", str(prompt))
    try:
        result = ai_client.chat_completion(messages=messages, temperature=0.1, max_tokens=800)
        if isinstance(result, dict) and result.get("summary"):
            return normalize_review_result(result)
        return {"status": "unavailable", "summary": "AI review could not be completed.", "findings": []}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Code review generation failed: %s", exc)
        return {"status": "unavailable", "summary": "AI review could not be completed.", "findings": []}
