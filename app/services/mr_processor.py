import logging
from typing import Any, Dict, List

from app.ai.code_reviewer import review_merge_request
from app.ai.mr_summary import generate_mr_summary
from app.ai.orchestrator import AIOrchestrator
from app.config import get_settings
from app.gitlab.client import GitLabClient
from app.services.description_manager import build_ai_unavailable_section, replace_ai_section
from app.validation.engine import ValidationEngine

logger = logging.getLogger(__name__)
EVENT_STATES: Dict[str, str] = {}
AI_REVIEW_NOTE_MARKER = "<!-- AI_REVIEW_NOTE -->"


def mark_event_pending(event_key: str) -> None:
    EVENT_STATES[event_key] = "pending"


def mark_event_processing(event_key: str) -> None:
    EVENT_STATES[event_key] = "processing"


def mark_event_completed(event_key: str) -> None:
    EVENT_STATES[event_key] = "completed"


def mark_event_failed(event_key: str) -> None:
    EVENT_STATES[event_key] = "failed"


def should_skip_duplicate(event_key: str) -> bool:
    return EVENT_STATES.get(event_key) in {"pending", "processing"}


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


def _format_ai_review_note(review: Dict[str, Any]) -> str:
    findings = review.get("findings") or []
    lines = [AI_REVIEW_NOTE_MARKER, "## AI Code Review", "", str(review.get("summary", "")).strip()]
    if findings:
        lines.extend(["", "### Findings"])
        for finding in findings:
            if isinstance(finding, dict):
                severity = str(finding.get("severity") or "unspecified").strip()
                file_name = str(finding.get("file") or finding.get("path") or "unknown").strip()
                detail = str(finding.get("message") or finding.get("description") or finding).strip()
                lines.append(f"- **{severity}** `{file_name}`: {detail}")
            else:
                lines.append(f"- {str(finding).strip()}")
    lines.append("")
    lines.append(AI_REVIEW_NOTE_MARKER)
    return "\n".join(lines)


def _publish_ai_review(client: GitLabClient, project_id: str, mr_iid: int, review: Dict[str, Any]) -> None:
    if review.get("status") == "unavailable":
        logger.warning("AI code review unavailable; no GitLab note created for %s/%s", project_id, mr_iid)
        return

    body = _format_ai_review_note(review)
    existing_notes = client.get_merge_request_notes(project_id, mr_iid)
    existing_note = next(
        (note for note in existing_notes if AI_REVIEW_NOTE_MARKER in str(note.get("body", ""))),
        None,
    )
    if existing_note and existing_note.get("id") is not None:
        client.update_merge_request_note(project_id, mr_iid, int(existing_note["id"]), body)
        logger.info("AI review note updated for %s !%s", project_id, mr_iid)
        return

    client.create_merge_request_note(project_id, mr_iid, body)
    logger.info("AI review note created for %s !%s", project_id, mr_iid)


async def process_gitlab_event(event_data: Dict[str, Any]) -> Dict[str, Any]:
    project_data = event_data.get("project", {})
    project_id = str(project_data.get("id") or event_data.get("project_id") or project_data.get("path_with_namespace") or "")
    mr_data = event_data.get("object_attributes", {})
    mr_iid = int(mr_data.get("iid") or mr_data.get("merge_request_iid") or 0)
    action = mr_data.get("action") or event_data.get("event_type") or "updated"

    if not project_id or not mr_iid:
        raise ValueError("GitLab event is missing project ID or MR IID")

    dedupe_key = f"{project_id}:{mr_iid}:{action}"
    mark_event_processing(dedupe_key)

    settings = get_settings()
    client = GitLabClient(base_url=settings.gitlab_url, token=settings.gitlab_token)
    context = build_mr_context(project_id, mr_iid, client)
    validation = ValidationEngine().validate(context)
    logger.info("Validation status for %s/%s: %s", project_id, mr_iid, validation["status"])

    if validation["status"] == "fail":
        logger.warning("Validation failed for project %s MR !%s", project_id, mr_iid)

    summary_orchestrator = AIOrchestrator()
    review_orchestrator = AIOrchestrator()
    ai_summary = generate_mr_summary(summary_orchestrator, context)
    ai_review = review_merge_request(review_orchestrator, context)

    description = context["merge_request"].get("description") or ""
    if ai_summary:
        description_content = ai_summary
        if ai_review.get("status") == "unavailable":
            description_content = ai_summary.replace(
                "<!-- AI_REVIEW_END -->",
                "\n### AI Review Status\nUnavailable. Human review is still required.\n<!-- AI_REVIEW_END -->",
            )
    else:
        description_content = build_ai_unavailable_section(summary_orchestrator.last_failure_reason, validation)
    updated_description = replace_ai_section(description, description_content)
    if updated_description != description:
        client.update_merge_request_description(project_id, mr_iid, updated_description)
        logger.info("MR description updated for %s !%s", project_id, mr_iid)

    _publish_ai_review(client, project_id, mr_iid, ai_review)

    status = {
        "project_id": project_id,
        "mr_iid": mr_iid,
        "action": action,
        "validation": validation,
        "summary": ai_summary,
        "summary_status": "available" if ai_summary else "unavailable",
        "review": ai_review,
    }
    mark_event_completed(dedupe_key)
    return status
