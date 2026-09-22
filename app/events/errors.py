import logging
import re
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

# Sensitive patterns to sanitize out of error messages
SENSITIVE_PATTERNS = [
    re.compile(r"glpat-[a-zA-Z0-9_\-]{20,}", re.IGNORECASE),
    re.compile(r"whsec_[a-zA-Z0-9_\-+/=]{10,}", re.IGNORECASE),
    re.compile(r"sk-[a-zA-Z0-9_\-]{20,}", re.IGNORECASE),
    re.compile(r"gsk_[a-zA-Z0-9_\-]{20,}", re.IGNORECASE),
    re.compile(r"Bearer\s+[a-zA-Z0-9_\-\.]+", re.IGNORECASE),
]


def sanitize_error_message(message: str, max_length: int = 1000) -> str:
    """Sanitize error messages to ensure no tokens or secrets leak into storage or logs."""
    if not message:
        return ""
    sanitized = str(message)
    for pattern in SENSITIVE_PATTERNS:
        sanitized = pattern.sub("[REDACTED_SECRET]", sanitized)
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "..."
    return sanitized


def classify_error(error: Exception) -> Tuple[str, bool]:
    """Classify an exception into an error category and determine if it is permanent.

    Returns:
        (category_name, is_permanent)
        where categories are one of:
        'transient', 'authentication', 'authorization', 'configuration',
        'validation', 'provider', 'gitlab', 'internal'
    """
    msg = str(error).lower()
    error_type = type(error).__name__.lower()

    # Authentication errors (permanent)
    if any(term in msg for term in ["401", "unauthorized", "invalid_token", "invalid gitlab token", "unauthorized_client"]):
        return "authentication", True

    # Authorization errors (permanent)
    if any(term in msg for term in ["403", "forbidden", "access_denied", "insufficient_scope"]):
        return "authorization", True

    # Configuration errors (permanent)
    if any(term in msg for term in ["missing configuration", "external ai providers prohibited", "not configured", "invalid_config"]):
        return "configuration", True

    # Validation / Malformed request errors (permanent)
    if any(term in msg for term in ["malformed", "invalid project", "invalid mr", "non-mr event", "missing project id"]):
        return "validation", True

    # Transient AI provider or network errors (retryable)
    if any(term in msg for term in ["429", "rate limit", "quota", "timeout", "timed out", "500", "502", "503", "504", "connection reset", "econnreset"]):
        return "transient", False

    # Provider errors
    if "openrouter" in msg or "groq" in msg or "ai" in msg:
        if any(term in msg for term in ["incomplete", "response", "parse", "json"]):
            return "provider", False
        return "provider", False

    # GitLab API errors
    if "gitlab" in msg:
        if "5" in msg:  # GitLab 5xx is transient
            return "gitlab", False
        return "gitlab", True

    # Generic transient fallback vs internal error
    if "timeout" in error_type or "connection" in error_type:
        return "transient", False

    return "internal", False
