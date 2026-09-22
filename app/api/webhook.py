import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from app.config import get_settings
from app.observability import log_event, record_metric
from app.services import mr_processor

logger = logging.getLogger(__name__)
router = APIRouter()


def verify_gitlab_webhook_signature(
    signing_token: str,
    webhook_id: str,
    webhook_timestamp: str,
    raw_body: bytes,
    received_signature: str,
) -> bool:
    """Verify GitLab's Standard Webhooks signing-token signature.

    GitLab signs: {webhook-id}.{webhook-timestamp}.{raw-body}
    using the decoded whsec_ signing token and HMAC-SHA256.
    """
    if not signing_token:
        raise ValueError("Missing signing token")
    if not webhook_id:
        raise ValueError("Missing webhook-id")
    if not webhook_timestamp:
        raise ValueError("Missing webhook-timestamp")
    if not received_signature:
        raise ValueError("Missing webhook-signature")

    if not signing_token.startswith("whsec_"):
        raise ValueError("Malformed GitLab signing token")

    token = signing_token[6:]
    try:
        signing_key = base64.b64decode(token, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid GitLab signing token base64") from exc

    payload = f"{webhook_id}.{webhook_timestamp}.".encode("utf-8") + raw_body
    digest = hmac.new(signing_key, payload, hashlib.sha256).digest()
    expected_signature = f"v1,{base64.b64encode(digest).decode('ascii')}"

    candidates = received_signature.split()
    for candidate in candidates:
        if not candidate.startswith("v1,"):
            continue
        if hmac.compare_digest(candidate, expected_signature):
            return True
    return False


def verify_gitlab_webhook_timestamp(webhook_timestamp: str, tolerance_seconds: int) -> bool:
    if not webhook_timestamp:
        return False
    try:
        timestamp_value = int(webhook_timestamp)
    except (TypeError, ValueError):
        return False

    current_time = int(time.time())
    return abs(current_time - timestamp_value) <= tolerance_seconds


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


def _actor_username(event_data: Dict[str, Any]) -> str:
    user = event_data.get("user") or event_data.get("user_data") or {}
    return str(user.get("username") or "").strip()


def _is_relevant_mr_event(event_data: Dict[str, Any], action: str) -> bool:
    normalized_action = action.lower()
    if normalized_action in {"open", "opened", "reopen", "reopened"}:
        return True
    if normalized_action not in {"update", "updated"}:
        return False

    changes = event_data.get("changes") or {}
    attributes = event_data.get("object_attributes") or {}
    return bool(
        changes.get("diffs")
        or changes.get("oldrev")
        or changes.get("newrev")
        or changes.get("source_branch")
        or changes.get("target_branch")
        or changes.get("title")
        or changes.get("description")
        or attributes.get("last_commit")
        or attributes.get("oldrev")
    )


async def _run_in_process_mr_processing(event_data: Dict[str, Any]) -> None:
    dedupe_key = event_data.get("_dedupe_key") or event_data.get("webhook_id") or "background"
    try:
        await mr_processor.process_gitlab_event(event_data)
        try:
            from app.events.store import EventStore
            from app.config import get_settings
            store = EventStore(get_settings().database_path)
            evt = store.get_by_dedupe_key(dedupe_key)
            if evt:
                store.complete(evt.event_id)
        except Exception:
            pass
    except Exception as exc:
        event_key = dedupe_key
        mr_processor.mark_event_failed(event_key)
        log_event("PROCESSING_FAILED", level="ERROR", event_type="merge_request", result="failed", webhook_id=event_data.get("webhook_id"))
        logger.exception("In-process MR processing failed safely")
        try:
            from app.events.store import EventStore
            from app.config import get_settings
            store = EventStore(get_settings().database_path)
            evt = store.get_by_dedupe_key(dedupe_key)
            if evt:
                store.fail(evt.event_id, exc)
        except Exception:
            pass


@router.post("/webhook/gitlab")
async def gitlab_webhook(request: Request) -> Dict[str, Any]:
    settings = get_settings()
    raw_body = await request.body()

    if len(raw_body) > settings.webhook_max_body_bytes:
        record_metric("webhook_rejected_total")
        log_event(
            "WEBHOOK_REJECTED",
            level="WARNING",
            event_type="merge_request",
            result="rejected",
            error_category="validation",
            reason="payload_too_large",
            size_bytes=len(raw_body),
        )
        raise HTTPException(status_code=413, detail="Payload Too Large")

    webhook_id = request.headers.get("webhook-id")
    webhook_timestamp = request.headers.get("webhook-timestamp")
    received_signature = request.headers.get("webhook-signature")

    if settings.gitlab_webhook_signing_token:
        if not webhook_id:
            record_metric("webhook_rejected_total")
            log_event("WEBHOOK_REJECTED", level="WARNING", event_type="merge_request", result="rejected", error_category="validation", reason="missing_webhook_id")
            raise HTTPException(status_code=401, detail="Missing webhook-id")
        if not webhook_timestamp:
            record_metric("webhook_rejected_total")
            log_event("WEBHOOK_REJECTED", level="WARNING", event_type="merge_request", result="rejected", error_category="validation", reason="missing_timestamp")
            raise HTTPException(status_code=401, detail="Missing webhook-timestamp")
        if not received_signature:
            record_metric("webhook_rejected_total")
            log_event("WEBHOOK_REJECTED", level="WARNING", event_type="merge_request", result="rejected", error_category="validation", reason="missing_signature")
            raise HTTPException(status_code=401, detail="Missing webhook-signature")
        if not verify_gitlab_webhook_timestamp(webhook_timestamp, settings.gitlab_webhook_timestamp_tolerance_seconds):
            record_metric("webhook_rejected_total")
            log_event("WEBHOOK_REJECTED", level="WARNING", event_type="merge_request", result="rejected", error_category="timeout", reason="stale_timestamp")
            raise HTTPException(status_code=401, detail="Invalid or expired webhook timestamp")
        try:
            verified = verify_gitlab_webhook_signature(
                settings.gitlab_webhook_signing_token,
                webhook_id,
                webhook_timestamp,
                raw_body,
                received_signature,
            )
        except ValueError as exc:
            record_metric("webhook_signature_failure_total")
            logger.warning("GitLab webhook signing verification rejected a malformed request")
            log_event("WEBHOOK_REJECTED", level="WARNING", event_type="merge_request", result="rejected", error_category="authentication", reason="malformed_signature")
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        if not verified:
            record_metric("webhook_signature_failure_total")
            log_event("WEBHOOK_REJECTED", level="WARNING", event_type="merge_request", result="rejected", error_category="authentication", reason="invalid_signature")
            raise HTTPException(status_code=401, detail="Invalid GitLab webhook signature")
    else:
        legacy_token = request.headers.get("X-Gitlab-Token")
        if legacy_token is None:
            record_metric("webhook_rejected_total")
            log_event("WEBHOOK_REJECTED", level="WARNING", event_type="merge_request", result="rejected", error_category="authentication", reason="missing_legacy_token")
            raise HTTPException(status_code=401, detail="Missing GitLab webhook token")
        secret = settings.gitlab_webhook_secret or ""
        if not secret:
            record_metric("webhook_rejected_total")
            log_event("WEBHOOK_REJECTED", level="WARNING", event_type="merge_request", result="rejected", error_category="configuration", reason="missing_legacy_secret")
            raise HTTPException(status_code=500, detail="GitLab webhook secret is not configured")
        if not hmac.compare_digest(legacy_token, secret):
            record_metric("webhook_signature_failure_total")
            log_event("WEBHOOK_REJECTED", level="WARNING", event_type="merge_request", result="rejected", error_category="authentication", reason="invalid_legacy_token")
            raise HTTPException(status_code=401, detail="Invalid GitLab webhook token")

    record_metric("webhook_received_total")
    log_event("WEBHOOK_RECEIVED", level="INFO", event_type="merge_request", result="received", webhook_id=webhook_id)
    try:
        event_data = await request.json()
    except Exception as exc:
        record_metric("webhook_rejected_total")
        logger.warning("Malformed GitLab webhook payload received")
        log_event("WEBHOOK_REJECTED", level="WARNING", event_type="merge_request", result="rejected", error_category="validation", reason="malformed_json")
        raise HTTPException(status_code=400, detail="Malformed JSON payload") from exc

    event_name = request.headers.get("X-Gitlab-Event") or "Merge Request Hook"
    try:
        mr_event = _extract_mr_event_metadata(event_data)
    except ValueError as exc:
        record_metric("webhook_rejected_total")
        logger.warning("Rejected non-MR GitLab webhook: %s", exc)
        log_event("WEBHOOK_REJECTED", level="WARNING", event_type="merge_request", result="rejected", error_category="validation", reason="non_mr_event")
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    actor_username = _actor_username(event_data)
    if actor_username == settings.gitlab_ai_username:
        record_metric("webhook_rejected_total")
        log_event("WEBHOOK_SKIPPED", level="INFO", event_type="merge_request", result="skipped", project_id=str(mr_event["project_id"]), mr_iid=mr_event["mr_iid"], reason="ai_reviewer_event")
        logger.info(
            "Skipping MR webhook generated by AI reviewer: event_id=%s project_id=%s mr_iid=%s action=%s actor_username=%s processing_decision=skip",
            request.headers.get("webhook-id") or request.headers.get("X-Gitlab-Event-UUID"),
            mr_event["project_id"],
            mr_event["mr_iid"],
            mr_event["action"],
            actor_username,
        )
        return {"status": "skipped", "event": event_name, "reason": "ai_reviewer_event"}

    if not _is_relevant_mr_event(event_data, str(mr_event["action"])):
        record_metric("webhook_rejected_total")
        log_event("WEBHOOK_SKIPPED", level="INFO", event_type="merge_request", result="skipped", project_id=str(mr_event["project_id"]), mr_iid=mr_event["mr_iid"], reason="irrelevant_event")
        logger.info(
            "Skipping irrelevant MR webhook: event_id=%s project_id=%s mr_iid=%s action=%s actor_username=%s processing_decision=skip",
            request.headers.get("webhook-id") or request.headers.get("X-Gitlab-Event-UUID"),
            mr_event["project_id"],
            mr_event["mr_iid"],
            mr_event["action"],
            actor_username,
        )
        return {"status": "skipped", "event": event_name, "reason": "irrelevant_event"}

    project_id = mr_event["project_id"]
    mr_iid = mr_event["mr_iid"]
    dedupe_key = (
        request.headers.get("webhook-id")
        or request.headers.get("X-Gitlab-Event-UUID")
        or f"{project_id}:{mr_iid}:{mr_event['action']}"
    )

    if mr_processor.should_skip_duplicate(dedupe_key):
        record_metric("webhook_duplicate_total")
        log_event("WEBHOOK_DUPLICATE", level="INFO", event_type="merge_request", result="duplicate", project_id=str(project_id), mr_iid=mr_iid, webhook_id=dedupe_key)
        logger.info("GitLab MR event is already queued or processing: %s", dedupe_key)
        return {"status": "duplicate", "event": event_name}

    event_data["webhook_id"] = dedupe_key
    event_data["_dedupe_key"] = dedupe_key
    mr_processor.mark_event_pending(dedupe_key)

    # Durable Event Store Persistence INSIDE the application
    from app.events.store import EventStore
    from app.events.manager import get_event_manager

    store = EventStore(settings.database_path)
    proj_path = (event_data.get("project") or {}).get("path_with_namespace")
    event_rec, created = store.enqueue(
        dedupe_key=dedupe_key,
        project_id=str(project_id),
        project_path=proj_path,
        mr_iid=mr_iid,
        action=str(mr_event["action"]),
        event_type=event_name,
        payload=event_data,
    )

    if not created:
        record_metric("webhook_duplicate_total")
        log_event("WEBHOOK_DUPLICATE", level="INFO", event_type="merge_request", result="duplicate", project_id=str(project_id), mr_iid=mr_iid, webhook_id=dedupe_key)
        return {"status": "duplicate", "event": event_name}

    record_metric("webhook_accepted_total")
    log_event(
        "EVENT_PERSISTED",
        level="INFO",
        event_type="merge_request",
        result="persisted",
        project_id=str(project_id),
        mr_iid=mr_iid,
        webhook_id=dedupe_key,
    )
    log_event(
        "WEBHOOK_ACCEPTED",
        level="INFO",
        event_type="merge_request",
        result="accepted",
        project_id=str(project_id),
        mr_iid=mr_iid,
        webhook_id=dedupe_key,
    )
    logger.info(
        "GitLab MR webhook persisted for in-process handling: event_id=%s project_id=%s mr_iid=%s action=%s actor_username=%s processing_decision=background",
        dedupe_key,
        project_id,
        mr_iid,
        mr_event["action"],
        actor_username,
    )

    # Schedule immediate in-process task and notify manager
    asyncio.create_task(_run_in_process_mr_processing(event_data))
    get_event_manager().notify_new_event()

    return {"status": "accepted", "event": event_name, "processing": "in_process", "webhook_id": dedupe_key}
