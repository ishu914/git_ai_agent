import hmac
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.config import get_settings
from app.services.mr_processor import (
    mark_event_failed,
    mark_event_pending,
    process_gitlab_event,
    should_skip_duplicate,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _extract_mr_event_metadata(event_data: Dict[str, Any]) -> Dict[str, Any]:
    object_kind = str(event_data.get("object_kind") or "").strip().lower()
    event_name = str(event_data.get("event_type") or "").strip().lower()
    if object_kind != "merge_request" and event_name != "merge_request":
        raise ValueError("Event is not a GitLab merge request webhook")

    project_data = event_data.get("project") or {}
    project_id = project_data.get("id") or event_data.get("project_id")
    object_attributes = event_data.get("object_attributes") or {}
    mr_iid = object_attributes.get("iid") or object_attributes.get("merge_request_iid")
    action = object_attributes.get("action") or event_name or "updated"

    if project_id is None or project_id == "":
        raise ValueError("Missing project ID in merge request webhook")
    if mr_iid is None:
        raise ValueError("Missing MR IID in merge request webhook")

    return {
        "project_id": project_id,
        "mr_iid": int(mr_iid),
        "action": action,
    }


async def _run_background_processing(event_data: Dict[str, Any], dedupe_key: str) -> None:
    logger.info("GitLab MR processing started for event %s", dedupe_key)
    try:
        await process_gitlab_event(event_data)
    except Exception:
        mark_event_failed(dedupe_key)
        logger.exception("GitLab MR processing failed for event %s", dedupe_key)
        return

    logger.info("GitLab MR processing completed for event %s", dedupe_key)


@router.post("/webhook/gitlab")
async def gitlab_webhook(request: Request, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    settings = get_settings()
    event_token = request.headers.get("X-Gitlab-Token")
    if event_token is None:
        raise HTTPException(status_code=401, detail="Missing GitLab webhook token")

    secret = settings.gitlab_webhook_secret or ""
    if not secret:
        raise HTTPException(status_code=500, detail="GitLab webhook secret is not configured")
    if not hmac.compare_digest(event_token, secret):
        raise HTTPException(status_code=401, detail="Invalid GitLab webhook token")

    try:
        event_data = await request.json()
    except Exception as exc:
        logger.warning("Malformed GitLab webhook payload received")
        raise HTTPException(status_code=400, detail="Malformed JSON payload") from exc

    event_name = request.headers.get("X-Gitlab-Event") or "unknown"
    try:
        mr_event = _extract_mr_event_metadata(event_data)
    except ValueError as exc:
        logger.warning("Rejected non-MR GitLab webhook: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    project_id = mr_event["project_id"]
    mr_iid = mr_event["mr_iid"]
    dedupe_key = request.headers.get("X-Gitlab-Event-UUID") or f"{project_id}:{mr_iid}:{mr_event['action']}"

    if should_skip_duplicate(dedupe_key):
        logger.info("GitLab MR event is already queued or processing: %s", dedupe_key)
        return {"status": "duplicate", "event": event_name}

    mark_event_pending(dedupe_key)
    logger.info("GitLab MR webhook accepted and queued - project=%s mr_iid=%s action=%s", project_id, mr_iid, mr_event["action"])
    background_tasks.add_task(_run_background_processing, event_data, dedupe_key)

    return {"status": "accepted", "event": event_name}
