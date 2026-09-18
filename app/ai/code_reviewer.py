import logging
from typing import Any, Dict

from app.ai.client import OpenRouterClient
from app.ai.prompts import build_messages_for_review
from app.ai.token_budget import deduplicate_findings

logger = logging.getLogger(__name__)


def normalize_review_result(result: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(result, dict) or not result.get("summary"):
        return {"status": "unavailable", "summary": "AI review could not be completed.", "findings": []}
    findings = result.get("findings", [])
    if isinstance(findings, dict):
        findings = [findings]
    if not isinstance(findings, list):
        findings = []
    findings = deduplicate_findings(findings)
    return {
        "status": str(result.get("status") or "reviewed"),
        "summary": str(result["summary"]).strip(),
        "findings": findings,
        "review_scope": result.get("review_scope", "full"),
        "excluded_files": result.get("excluded_files", []),
        "breaking_changes": result.get("breaking_changes", []),
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
