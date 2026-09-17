import hashlib
import hmac
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from app.config import get_settings
from app.services.mr_processor import process_gitlab_event

logger = logging.getLogger(__name__)
router = APIRouter()


def _verify_gitlab_signature(payload: bytes, secret: Optional[str]) -> bool:
    if not secret:
        logger.warning("GitLab webhook secret not configured; rejecting request")
        return False
    provided = None
    try:
        provided = payload
    except Exception:
        provided = payload

    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    header_value = expected
    return header_value == expected


@router.post("/webhook/gitlab")
async def gitlab_webhook(request: Request) -> Dict[str, Any]:
    settings = get_settings()
    body = await request.body()
    event_token = request.headers.get("X-Gitlab-Token")
    if not event_token:
        raise HTTPException(status_code=401, detail="Missing GitLab webhook signature")

    secret = settings.gitlab_webhook_secret or ""
    if secret and not hmac.compare_digest(event_token, secret):
        raise HTTPException(status_code=401, detail="Invalid GitLab webhook signature")
    if not secret:
        raise HTTPException(status_code=500, detail="Webhook secret is not configured")

    payload = body
    event_uuid = request.headers.get("X-Gitlab-Event-UUID")
    event_name = request.headers.get("X-Gitlab-Event") or "unknown"

    try:
        event_data = await request.json()
    except Exception as exc:
        logger.warning("Malformed GitLab webhook payload: %s", exc)
        raise HTTPException(status_code=400, detail="Malformed JSON payload") from exc

    dedupe_key = event_uuid or event_data.get("object_attributes", {}).get("id") or event_data.get("event_type")
    if not dedupe_key:
        raise HTTPException(status_code=400, detail="Missing event identifier")

    if process_gitlab_event.is_duplicate(dedupe_key):
        logger.info("Ignoring duplicate GitLab event %s", dedupe_key)
        return {"status": "duplicate", "event": event_name}

    process_gitlab_event.mark_processed(dedupe_key)

    try:
        await process_gitlab_event(event_data)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("GitLab event processing failed for %s", dedupe_key)
        return {"status": "error", "message": str(exc)}

    return {"status": "accepted", "event": event_name}
