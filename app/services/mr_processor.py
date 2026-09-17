import asyncio
import logging
from typing import Any, Dict, List, Optional

from app.ai.client import OpenRouterClient
from app.ai.code_reviewer import review_merge_request
from app.ai.mr_summary import generate_mr_summary
from app.config import get_settings
from app.gitlab.client import GitLabClient
from app.validation.engine import ValidationEngine

logger = logging.getLogger(__name__)
PROCESSED_EVENTS: Dict[str, str] = {}


def build_mr_context(project_id: str, mr_iid: int, client: GitLabClient) -> Dict[str, Any]:
    project = client.get_project(project_id)
    mr = client.get_merge_request(project_id, mr_iid)
    changes = client.get_merge_request_changes(project_id, mr_iid)
    commits = client.get_merge_request_commits(project_id, mr_iid)
    approvals = client.get_merge_request_approvals(project_id, mr_iid)

    changed_files = []
    for item in changes.get("changes", []):
        file_path = item.get("new_path") or item.get("old_path") or "unknown"
        changed_files.append({
            "path": file_path,
            "status": item.get("new_file") and "added" or item.get("deleted_file") and "deleted" or "modified",
            "additions": item.get("additions", 0),
            "deletions": item.get("deletions", 0),
        })

    total_additions = sum(int(entry.get("additions", 0)) for entry in changed_files)
    total_deletions = sum(int(entry.get("deletions", 0)) for entry in changed_files)
    total_diff_size = sum(int(item.get("diff", "").__len__()) for item in changes.get("changes", []))

    return {
        "project": project,
        "merge_request": mr,
        "changes": changes,
        "commits": commits,
        "approvals": approvals,
        "changed_files": changed_files,
        "total_additions": total_additions,
        "total_deletions": total_deletions,
        "total_diff_size": total_diff_size,
    }


async def process_gitlab_event(event_data: Dict[str, Any]) -> Dict[str, Any]:
    project_data = event_data.get("project", {})
    project_id = str(project_data.get("id") or event_data.get("project_id") or project_data.get("path_with_namespace") or "")
    mr_data = event_data.get("object_attributes", {})
    mr_iid = int(mr_data.get("iid") or mr_data.get("merge_request_iid") or 0)
    action = mr_data.get("action") or event_data.get("event_type") or "updated"

    if not project_id or not mr_iid:
        raise ValueError("GitLab event is missing project ID or MR IID")

    settings = get_settings()
    client = GitLabClient(base_url=settings.gitlab_url, token=settings.gitlab_token)
    context = build_mr_context(project_id, mr_iid, client)
    validation = ValidationEngine().validate(context)
    logger.info("Validation status for %s/%s: %s", project_id, mr_iid, validation["status"])

    if validation["status"] == "fail":
        logger.warning("Validation failed for project %s MR !%s", project_id, mr_iid)

    ai_client = OpenRouterClient()
    ai_summary = generate_mr_summary(ai_client, context)
    ai_review = review_merge_request(ai_client, context)

    if ai_summary:
        description = context["merge_request"].get("description") or ""
        updated_description = description
        if "<!-- AI_REVIEW_START -->" not in description:
            updated_description = f"{description}\n\n{ai_summary}" if description.strip() else ai_summary
        client.update_merge_request_description(project_id, mr_iid, updated_description)
        logger.info("MR description updated for %s !%s", project_id, mr_iid)

    status = {
        "project_id": project_id,
        "mr_iid": mr_iid,
        "action": action,
        "validation": validation,
        "summary": ai_summary,
        "review": ai_review,
    }
    return status


def is_duplicate(event_key: str) -> bool:
    global PROCESSED_EVENTS
    return event_key in PROCESSED_EVENTS


def mark_processed(event_key: str) -> None:
    global PROCESSED_EVENTS
    PROCESSED_EVENTS[event_key] = "processed"


process_gitlab_event.is_duplicate = is_duplicate
process_gitlab_event.mark_processed = mark_processed
