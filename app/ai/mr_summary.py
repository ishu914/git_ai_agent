import logging
from typing import Any, Dict, List

from app.ai.prompts import build_messages_for_review
from app.ai.client import OpenRouterClient
from app.services.description_manager import build_ai_section

logger = logging.getLogger(__name__)


def generate_mr_summary(ai_client: OpenRouterClient, mr_context: Dict[str, Any]) -> str:
    mr = mr_context.get("merge_request", {})
    project = mr_context.get("project", {})
    changes = mr_context.get("changes", {}).get("changes", [])
    file_names = [item.get("new_path") or item.get("old_path") or "unknown" for item in changes]

    prompt = {
        "summary": "Generate a structured summary for this GitLab merge request.",
        "project": project.get("path_with_namespace", "unknown"),
        "title": mr.get("title", ""),
        "description": mr.get("description", ""),
        "source_branch": mr.get("source_branch", ""),
        "target_branch": mr.get("target_branch", ""),
        "file_count": len(file_names),
        "files": file_names[:20],
        "diff_preview": "\n".join((entry.get("diff") or "")[:1200] for entry in changes[:10]),
    }

    messages = build_messages_for_review("mr-summary", str(prompt))
    try:
        result = ai_client.chat_completion(messages=messages, temperature=0.2, max_tokens=600)
        if not isinstance(result, dict):
            return ""
        ai_section = build_ai_section({
            "summary": result.get("summary", "No summary generated."),
            "change_type": result.get("change_type", []),
            "testing": result.get("testing", "Testing not specified."),
            "risk": result.get("risk", "low"),
            "files_summary": result.get("files_summary", file_names[:10]),
        })
        return ai_section
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("MR summary generation failed: %s", exc)
        return ""
