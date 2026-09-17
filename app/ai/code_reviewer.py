import logging
from typing import Any, Dict

from app.ai.client import OpenRouterClient
from app.ai.prompts import build_messages_for_review

logger = logging.getLogger(__name__)


def review_merge_request(ai_client: OpenRouterClient, mr_context: Dict[str, Any]) -> Dict[str, Any]:
    mr = mr_context.get("merge_request", {})
    changes = mr_context.get("changes", {}).get("changes", [])
    diff_preview = "\n".join((entry.get("diff") or "")[:1500] for entry in changes[:20])

    prompt = {
        "title": mr.get("title", ""),
        "source_branch": mr.get("source_branch", ""),
        "target_branch": mr.get("target_branch", ""),
        "diff": diff_preview,
        "focus": "Review for bugs, security, auth, secrets, performance, error handling, maintainability, tests, and dangerous changes. Return valid JSON only.",
    }

    messages = build_messages_for_review("code-review", str(prompt))
    try:
        result = ai_client.chat_completion(messages=messages, temperature=0.1, max_tokens=800)
        if isinstance(result, dict):
            return result
        return {"status": "unavailable", "summary": "AI review could not be completed.", "findings": []}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Code review generation failed: %s", exc)
        return {"status": "unavailable", "summary": "AI review could not be completed.", "findings": []}
