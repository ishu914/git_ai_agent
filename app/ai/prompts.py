"""Prompt-building helpers for AI operations."""

from typing import Any, Dict, List


def build_system_prompt(task: str) -> str:
    return (
        "You are a careful GitLab review assistant. Do not approve or merge MRs. "
        "Return only compact valid JSON matching the requested schema. "
        "Do not reveal secrets or tokens. "
        f"Task: {task}"
    )


def build_json_message(role: str, content: Dict[str, Any] | str) -> Dict[str, Any]:
    return {"role": role, "content": content}


def build_messages_for_review(task: str, prompt_body: str) -> List[Dict[str, Any]]:
    return [
        build_json_message("system", build_system_prompt(task)),
        build_json_message("user", prompt_body),
    ]
