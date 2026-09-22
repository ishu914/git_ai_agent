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
        return [str(item).strip() for item in value if str(item).strip() and not isinstance(item, dict)]
    return []


def build_ai_section(data: Dict[str, Any]) -> str:
    summary = _as_text(data.get("summary"), "AI summary unavailable.")
    change_type = ", ".join(_as_text_list(data.get("change_type"))) or "Not specified"
    testing = _as_text(data.get("testing"), "Not specified")
    risk = _as_text(data.get("risk"), "Not specified").lower()
    files_summary = "\n".join(f"- {item}" for item in _as_text_list(data.get("files_summary")))
    review_scope = _as_text(data.get("review_scope"), "full")
    excluded_files = ", ".join(_as_text_list(data.get("excluded_files")))
    raw_breaking_changes = data.get("breaking_changes")
    if isinstance(raw_breaking_changes, list):
        breaking_changes = "none identified" if not raw_breaking_changes else "potential"
    else:
        breaking_changes = _as_text(raw_breaking_changes, "none identified")
    reviewer_attention = _as_text_list(data.get("reviewer_attention"))
    findings = data.get("findings") or []
    finding_lines = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        severity = _as_text(finding.get("severity"), "info")
        category = _as_text(finding.get("category"), "info")
        issue = _as_text(finding.get("issue"), "")
        if category == severity:
            category = "info" if severity != "info" else "security" if any(marker in issue.lower() for marker in ("secret", "credential", "token", "password", "access key")) else "info"
        file_name = _as_text(finding.get("file"), "unknown")
        line = finding.get("line")
        location = f"{file_name}:{line}" if isinstance(line, int) and line > 0 else file_name
        recommendation = _as_text(finding.get("recommendation"), "")
        finding_lines.append(f"- **{severity} / {category}** `{location}` {issue}" + (f" **Recommendation:** {recommendation}" if recommendation else ""))

    return (
        f"{AI_SECTION_START}\n"
        "## AI Generated Summary\n\n"
        f"### Summary\n{summary}\n\n"
        f"### Change Type\n{change_type or 'Not specified'}\n\n"
        f"### Files Changed\n{files_summary or 'No file summary available.'}\n\n"
        f"### Testing\n{testing or 'Testing not specified.'}\n\n"
        f"### Risk\n{risk or 'low'}\n"
        f"### Breaking Changes\n{breaking_changes}\n"
        "### Reviewer Attention\n"
        f"{chr(10).join(f'- {item}' for item in reviewer_attention) if reviewer_attention else 'No additional reviewer attention identified.'}\n"
        "### Findings\n"
        f"{chr(10).join(finding_lines) if finding_lines else 'No actionable findings identified.'}\n"
        f"### Review Scope\n{review_scope}\n"
        f"{f'Excluded: {excluded_files}\n' if excluded_files else ''}"
        f"{AI_SECTION_END}"
    )


def build_ai_unavailable_section(reason: str, validation: Dict[str, Any]) -> str:
    validation_status = str(validation.get("status", "unknown")).strip()
    return (
        f"{AI_SECTION_START}\n"
        "## AI Review\n\n"
        "### AI Status\nUnavailable\n\n"
        f"### Reason\n{_as_text(reason, 'All configured AI providers/models failed to return a usable response.')}\n\n"
        f"### Deterministic Validation\n{validation_status}\n\n"
        "AI analysis was unavailable. Human review is still required.\n"
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
