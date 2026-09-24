"""Fixed, safe-to-publish AI unavailability statuses."""

from typing import Iterable


NO_PROVIDER = "AI review unavailable: no AI provider is configured."
RATE_LIMITED = "AI review unavailable: usage limits reached on configured models."
UNREACHABLE = "AI review unavailable: configured AI provider is not reachable."
UNUSABLE = "AI review unavailable: models did not return a usable response."
TIMEOUT = "AI review unavailable: AI provider did not respond in time."
TEMPORARY = "AI review unavailable: temporary issue, will retry on next update."
PUBLIC_STATUSES = {NO_PROVIDER, RATE_LIMITED, UNREACHABLE, UNUSABLE, TIMEOUT, TEMPORARY}


def classify_ai_failures(errors: Iterable[Exception], no_provider: bool = False) -> str:
    """Classify aggregate fallback failure without exposing provider internals."""
    if no_provider:
        return NO_PROVIDER
    messages = [str(error).lower() for error in errors]
    if not messages:
        return TEMPORARY
    if all(any(token in message for token in ("429", "rate limit", "quota", "usage limit")) for message in messages):
        return RATE_LIMITED
    if all(any(token in message for token in ("401", "403", "auth", "unauthorized", "forbidden", "api key", "configuration")) for message in messages):
        return UNREACHABLE
    if all(any(token in message for token in ("incomplete", "malformed", "invalid json", "empty content", "usable response")) for message in messages):
        return UNUSABLE
    if all(any(token in message for token in ("timeout", "timed out", "connect", "network")) for message in messages):
        return TIMEOUT
    return TEMPORARY


def public_ai_failure_status(reason: str) -> str:
    """Allow only fixed statuses into GitLab-visible AI-unavailable content."""
    return reason if reason in PUBLIC_STATUSES else TEMPORARY
