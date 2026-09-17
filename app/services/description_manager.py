from typing import Any, Dict


AI_SECTION_START = "<!-- AI_REVIEW_START -->"
AI_SECTION_END = "<!-- AI_REVIEW_END -->"


def build_ai_section(data: Dict[str, Any]) -> str:
    summary = str(data.get("summary", "")).strip()
    change_type = ", ".join(data.get("change_type", []) or [])
    testing = str(data.get("testing", "")).strip()
    risk = str(data.get("risk", "low")).strip().lower()
    files_summary = "\n".join(f"- {item}" for item in (data.get("files_summary") or []))

    return (
        f"{AI_SECTION_START}\n"
        "## AI Generated Summary\n\n"
        f"### Summary\n{summary or 'No summary provided.'}\n\n"
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
