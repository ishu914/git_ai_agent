import logging
from typing import Any, Dict, List

from app.ai.prompts import build_messages_for_review
from app.ai.client import OpenRouterClient
from app.services.description_manager import build_ai_section

logger = logging.getLogger(__name__)


def render_mr_summary(result: Dict[str, Any]) -> str:
    if not isinstance(result, dict) or not result.get("summary"):
        return ""
    return build_ai_section({
        "summary": result.get("summary"),
        "change_type": result.get("change_type"),
        "testing": result.get("testing"),
        "risk": result.get("risk"),
        "files_summary": result.get("files_summary"),
        "review_scope": result.get("review_scope", "full"),
        "excluded_files": result.get("excluded_files", []),
    })


def generate_mr_summary(ai_client: OpenRouterClient, mr_context: Dict[str, Any]) -> str:
    mr = mr_context.get("merge_request", {})
    project = mr_context.get("project", {})
    changes = mr_context.get("changes", {}).get("changes", [])
    file_names = [item.get("new_path") or item.get("old_path") or "unknown" for item in changes]

    prompt = {
        "title": mr.get("title", ""),
        "source_branch": mr.get("source_branch", ""),
        "target_branch": mr.get("target_branch", ""),
        "files": file_names[:20],
        "instruction": "Return JSON with summary, change_type, files_summary, testing, risk. Keep every value concise.",
    }

    messages = build_messages_for_review("mr-summary", str(prompt))
    try:
        result = ai_client.chat_completion(messages=messages, temperature=0.2, max_tokens=600)
        if not isinstance(result, dict) or not result.get("summary"):
            logger.warning("MR summary generation unavailable: missing summary field")
            return ""
        return render_mr_summary(result)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("MR summary generation failed: %s", exc)
        return ""
