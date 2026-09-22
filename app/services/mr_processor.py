import logging
from typing import Any, Dict, List

from app.ai.code_reviewer import normalize_review_result
from app.ai.mr_summary import render_mr_summary
from app.ai.orchestrator import AIOrchestrator
from app.config import get_settings
from app.gitlab.client import GitLabClient
from app.observability import log_event
from app.services.description_manager import build_ai_unavailable_section, replace_ai_section
from app.validation.engine import ValidationEngine

logger = logging.getLogger(__name__)
EVENT_STATES: Dict[str, str] = {}
AI_REVIEW_NOTE_MARKER = "<!-- AI_REVIEW_NOTE -->"


def _diff_statistics(diff: Any) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in str(diff or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions


def _file_statistics(item: Dict[str, Any]) -> tuple[int, int]:
    diff_additions, diff_deletions = _diff_statistics(item.get("diff"))
    additions = int(item["additions"]) if "additions" in item and item.get("additions") is not None else diff_additions
    deletions = int(item["deletions"]) if "deletions" in item and item.get("deletions") is not None else diff_deletions
    return additions, deletions


def mark_event_pending(event_key: str) -> None:
    EVENT_STATES[event_key] = "pending"


def mark_event_processing(event_key: str) -> None:
    EVENT_STATES[event_key] = "processing"


def mark_event_completed(event_key: str) -> None:
    EVENT_STATES[event_key] = "completed"


def mark_event_failed(event_key: str) -> None:
    EVENT_STATES[event_key] = "failed"


def should_skip_duplicate(event_key: str) -> bool:
    if EVENT_STATES.get(event_key) in {"pending", "processing", "completed"}:
        return True
    try:
        from app.events.store import EventStore
        from app.config import get_settings
        store = EventStore(get_settings().database_path)
        evt = store.get_by_dedupe_key(event_key)
        if evt and evt.status in {"pending", "processing", "completed", "PENDING", "PROCESSING", "COMPLETED"}:
            return True
    except Exception:
        pass
    return False


def build_mr_context(project_id: str, mr_iid: int, client: GitLabClient) -> Dict[str, Any]:
    project = client.get_project(project_id)
    mr = client.get_merge_request(project_id, mr_iid)
    changes = client.get_merge_request_changes(project_id, mr_iid)
    commits = client.get_merge_request_commits(project_id, mr_iid)
    approvals = client.get_merge_request_approvals(project_id, mr_iid)

    changed_files = []
    for item in changes.get("changes", []):
        file_path = item.get("new_path") or item.get("old_path") or "unknown"
        additions, deletions = _file_statistics(item)
        changed_files.append({
            "path": file_path,
            "status": item.get("new_file") and "added" or item.get("deleted_file") and "deleted" or "modified",
            "additions": additions,
            "deletions": deletions,
            "old_path": item.get("old_path"),
            "binary": bool(item.get("binary") or item.get("is_binary")),
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
                category = str(finding.get("category") or "info").strip()
                line = finding.get("line")
                location = f"{file_name}:{line}" if isinstance(line, int) and line > 0 else file_name
                detail = str(finding.get("issue") or finding.get("message") or finding.get("description") or "").strip()
                recommendation = str(finding.get("recommendation") or "").strip()
                suffix = f" Recommendation: {recommendation}" if recommendation else ""
                lines.append(f"- **{severity} / {category}** `{location}`: {detail}{suffix}")
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
    event_id = event_data.get("webhook_id") or event_data.get("event_id") or "background"
    dedupe_key = event_data.get("_dedupe_key") or f"{project_id}:{mr_iid}:{action}"

    if not project_id or not mr_iid:
        raise ValueError("GitLab event is missing project ID or MR IID")

    logger.info(
        "MR processing started: event_id=%s project_id=%s mr_iid=%s action=%s processing_decision=process",
        event_id,
        project_id,
        mr_iid,
        action,
    )

    mark_event_processing(dedupe_key)
    log_event("PROCESSING_STARTED", level="INFO", event_type="merge_request", result="started", project_id=project_id, mr_iid=mr_iid, webhook_id=event_id)

    settings = get_settings()
    client = GitLabClient(base_url=settings.gitlab_url, token=settings.gitlab_token)
    log_event("GITLAB_MR_FETCH", level="INFO", event_type="merge_request", result="started", project_id=project_id, mr_iid=mr_iid, webhook_id=event_id)
    context = build_mr_context(project_id, mr_iid, client)
    log_event("GITLAB_DIFF_FETCH", level="INFO", event_type="merge_request", result="completed", project_id=project_id, mr_iid=mr_iid, webhook_id=event_id)
    validation = ValidationEngine().validate(context)
    log_event("VALIDATION_COMPLETE", level="INFO", event_type="merge_request", result=validation["status"], project_id=project_id, mr_iid=mr_iid, webhook_id=event_id)
    logger.info("Validation status for %s/%s: %s", project_id, mr_iid, validation["status"])

    if validation["status"] == "fail":
        logger.warning("Validation failed for project %s MR !%s", project_id, mr_iid)

    from app.project_config import get_project_settings
    proj_path = project_data.get("path_with_namespace") or str(project_id)
    project_settings = get_project_settings(project_path=proj_path)

    ai_orchestrator = AIOrchestrator()
    try:
        log_event("AI_REVIEW_STARTED", level="INFO", event_type="merge_request", result="started", project_id=project_id, mr_iid=mr_iid, webhook_id=event_id)
        try:
            analysis = ai_orchestrator.analyze_mr(project_id, mr_iid, context, validation, project_settings=project_settings)
        except TypeError:
            analysis = ai_orchestrator.analyze_mr(project_id, mr_iid, context, validation)
        analysis = dict(analysis)
        analysis["deterministic_files"] = [
            f"{item['path']} (+{item['additions']} / -{item['deletions']})"
            for item in context["changed_files"]
        ]
        log_event("AI_REVIEW_COMPLETED", level="INFO", event_type="merge_request", result="completed", project_id=project_id, mr_iid=mr_iid, webhook_id=event_id, provider=analysis.get("provider"), model=analysis.get("model"))
        ai_review = normalize_review_result(analysis, changed_files=context["changed_files"])
        analysis.update({
            "testing": ai_review.get("testing", ""),
            "breaking_changes": ai_review.get("breaking_changes", "unknown"),
            "findings": ai_review.get("findings", []),
            "risk": ai_review.get("risk", "unknown"),
            "change_type": ai_review.get("change_type", "unknown"),
            "reviewer_attention": ai_review.get("reviewer_attention", []),
        })
        ai_summary = render_mr_summary(analysis)
    except Exception as exc:
        logger.warning(
            "AI analysis unavailable: project_id=%s mr_iid=%s error=%s",
            project_id,
            mr_iid,
            str(exc)[:200],
        )
        ai_summary = ""
        ai_review = {"status": "unavailable", "summary": "AI review could not be completed.", "findings": []}

    description = context["merge_request"].get("description") or ""
    if ai_summary:
        description_content = ai_summary
        if ai_review.get("status") == "unavailable":
            description_content = ai_summary.replace(
                "<!-- AI_REVIEW_END -->",
                "\n### AI Review Status\nUnavailable. Human review is still required.\n<!-- AI_REVIEW_END -->",
            )
    else:
        description_content = build_ai_unavailable_section(ai_orchestrator.last_failure_reason, validation)
    updated_description = replace_ai_section(description, description_content)
    if updated_description != description:
        client.update_merge_request_description(project_id, mr_iid, updated_description)
        log_event("DESCRIPTION_UPDATE", level="INFO", event_type="merge_request", result="completed", project_id=project_id, mr_iid=mr_iid, webhook_id=event_id)
        logger.info("MR description updated for %s !%s", project_id, mr_iid)

    _publish_ai_review(client, project_id, mr_iid, ai_review)
    log_event("REVIEW_NOTE", level="INFO", event_type="merge_request", result=ai_review.get("status"), project_id=project_id, mr_iid=mr_iid, webhook_id=event_id)

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
    log_event("PROCESSING_COMPLETED", level="INFO", event_type="merge_request", result="completed", project_id=project_id, mr_iid=mr_iid, webhook_id=event_id)
    return status
