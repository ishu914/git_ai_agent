import re
from typing import Any, Dict, Iterable, Optional

from app.ai.token_budget import redact_sensitive_text


DEFAULT_PLACEHOLDERS = {"update", "changes", "wip", "initial commit", "default commit message"}


def _message_text(commit: Any) -> str:
    if isinstance(commit, dict):
        return str(commit.get("message") or commit.get("title") or "").strip()
    return str(commit or "").strip()


def is_meaningful_commit_message(message: str, placeholders: Iterable[str] = ()) -> bool:
    normalized = " ".join(message.lower().split())
    configured = {" ".join(str(item).lower().split()) for item in placeholders if str(item).strip()}
    return bool(normalized) and normalized not in DEFAULT_PLACEHOLDERS and normalized not in configured and len(normalized) >= 12


def should_generate_commit_message(commits: Iterable[Any], config: Optional[Dict[str, Any]] = None) -> bool:
    policy = config or {}
    if not policy.get("enabled") or not policy.get("generate_when_default"):
        return False
    placeholders = policy.get("placeholders") or []
    messages = [_message_text(commit) for commit in commits]
    return bool(messages) and all(not is_meaningful_commit_message(message, placeholders) for message in messages)


def normalize_commit_message(value: Any) -> Optional[str]:
    message = redact_sensitive_text(str(value or "")).strip().splitlines()[0][:120]
    if not message or "[REDACTED]" in message or re.search(r"(?:AKIA[0-9A-Z]{16}|gh[pousr]_\w+|sk-[A-Za-z0-9_-]+)", message):
        return None
    if re.search(r"\b(?:ticket|issue|jira)[- #]?[A-Z]+-\d+\b|\b[A-Z]{2,}-\d+\b", message, re.IGNORECASE):
        return None
    return message


def select_commit_message(commits: Iterable[Any], generated: Any, config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    policy = config or {}
    messages = [_message_text(commit) for commit in commits]
    if not policy.get("enabled") or not policy.get("generate_when_default") or not should_generate_commit_message(messages, policy):
        return messages[0] if messages else None
    return normalize_commit_message(generated) or (messages[0] if messages else None)