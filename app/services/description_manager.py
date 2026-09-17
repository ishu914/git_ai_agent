from typing import Any, Dict


AI_SECTION_START = "<!-- AI_REVIEW_START -->"
AI_SECTION_END = "<!-- AI_REVIEW_END -->"


def _as_text(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return default
    return str(value).strip()


def _as_text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def build_ai_section(data: Dict[str, Any]) -> str:
    summary = _as_text(data.get("summary"), "AI summary unavailable.")
    change_type = ", ".join(_as_text_list(data.get("change_type"))) or "Not specified"
    testing = _as_text(data.get("testing"), "Not specified")
    risk = _as_text(data.get("risk"), "Not specified").lower()
    files_summary = "\n".join(f"- {item}" for item in _as_text_list(data.get("files_summary")))

    return (
        f"{AI_SECTION_START}\n"
        "## AI Generated Summary\n\n"
        f"### Summary\n{summary}\n\n"
        f"### Change Type\n{change_type or 'Not specified'}\n\n"
        f"### Files Changed\n{files_summary or 'No file summary available.'}\n\n"
        f"### Testing\n{testing or 'Testing not specified.'}\n\n"
        f"### Risk\n{risk or 'low'}\n"
        f"{AI_SECTION_END}"
    )


def replace_ai_section(existing_description: str, replacement: str) -> str:
    start = existing_description.find(AI_SECTION_START)
    end = existing_description.find(AI_SECTION_END)

    if start == -1 or end == -1 or end < start:
        if existing_description.strip():
            return f"{existing_description.rstrip()}\n\n{replacement}\n"
        return f"{replacement}\n"

    before = existing_description[:start].rstrip()
    after = existing_description[end + len(AI_SECTION_END):].lstrip()
    return f"{before}\n\n{replacement}\n\n{after}".strip() + "\n"
