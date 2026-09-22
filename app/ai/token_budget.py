import hashlib
import json
import os
import re
from typing import Any, Dict, Iterable, List, Tuple


LOW_VALUE_PARTS = (".git", "node_modules", "vendor", "dist", "build", "coverage")
LOW_VALUE_NAMES = ("lock", ".min.", ".map", ".bundle")
SOURCE_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rb", ".php", ".sql", ".yaml", ".yml", ".json", ".toml", ".sh"}
SECURITY_HINTS = ("auth", "login", "permission", "secret", "token", "password", "crypto", "security", "api", "sql", "docker", ".env")
REVIEW_POLICY_VERSION = "phase8-quality-v1"
SECRET_PATTERNS = (
    re.compile(r"(?i)((?:authorization|auth|password|passwd|token|api[_-]?key|secret|access[_-]?key|private[_-]?key|aws_secret_access_key))\s*[:=]\s*(?:bearer\s+)?([^\s,;\]\)\}\"']+)"),
    re.compile(r"(?i)\b(?:bearer)\s+([A-Za-z0-9_\-\.]{6,})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{6,}\b"),
    re.compile(r"\bgho_[A-Za-z0-9]{6,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{6,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b"),
)


def redact_sensitive_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def estimate_tokens(value: Any) -> int:
    if isinstance(value, str):
        return max(1, (len(value) + 3) // 4)
    return estimate_tokens(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str))


def _is_relevant_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = os.path.basename(normalized)
    if any(part in normalized.split("/") for part in LOW_VALUE_PARTS):
        return False
    if any(marker in name for marker in LOW_VALUE_NAMES):
        return False
    return True


def _priority(path: str, diff: str) -> Tuple[int, str]:
    lower = path.lower()
    extension = os.path.splitext(lower)[1]
    score = 0
    if extension in SOURCE_EXTENSIONS:
        score += 30
    if any(hint in lower for hint in SECURITY_HINTS):
        score += 40
    if "binary" in diff.lower() or not diff.strip():
        score -= 100
    return (-score, path)


def build_compact_review_payload(
    mr_context: Dict[str, Any],
    validation: Dict[str, Any],
    max_diff_chars: int,
    max_file_chars: int,
    max_context_files: int,
) -> Tuple[Dict[str, Any], bool, List[str]]:
    mr = mr_context.get("merge_request") or {}
    changes = (mr_context.get("changes") or {}).get("changes") or []
    selected = []
    excluded = []
    for entry in changes:
        path = str(entry.get("new_path") or entry.get("old_path") or "unknown")
        diff = redact_sensitive_text(str(entry.get("diff") or ""))
        if entry.get("binary") or entry.get("is_binary") or not _is_relevant_path(path) or len(selected) >= max_context_files:
            excluded.append(path)
            continue
        selected.append((path, diff))
    selected.sort(key=lambda item: _priority(item[0], item[1]))

    remaining = max_diff_chars
    compact_changes = []
    for path, diff in selected:
        if remaining <= 0:
            excluded.append(path)
            continue
        clipped = diff[: min(max_file_chars, remaining)]
        remaining -= len(clipped)
        compact_changes.append({"file": path, "diff": clipped})
        if len(clipped) < len(diff):
            excluded.append(f"{path} (truncated)")

    file_facts = []
    for entry in changes:
        old_path = str(entry.get("old_path") or "")
        new_path = str(entry.get("new_path") or "")
        file_facts.append({
            "path": new_path or old_path or "unknown",
            "old_path": old_path or None,
            "status": "renamed" if old_path and new_path and old_path != new_path else "added" if entry.get("new_file") else "deleted" if entry.get("deleted_file") else "modified",
            "additions": int(entry.get("additions") or 0),
            "deletions": int(entry.get("deletions") or 0),
            "binary": bool(entry.get("binary") or entry.get("is_binary")),
            "test_file": _is_test_path(new_path or old_path),
        })

    commits = []
    for commit in mr_context.get("commits") or []:
        if isinstance(commit, dict):
            message = redact_sensitive_text(str(commit.get("title") or commit.get("message") or "")).strip()
            if message:
                commits.append(message[:500])
        elif str(commit).strip():
            commits.append(redact_sensitive_text(str(commit).strip())[:500])

    payload = {
        "title": redact_sensitive_text(str(mr.get("title") or ""))[:500],
        "description": redact_sensitive_text(str(mr.get("description") or ""))[:1000],
        "source_branch": str(mr.get("source_branch") or ""),
        "target_branch": str(mr.get("target_branch") or ""),
        "commits": commits[:20],
        "file_facts": file_facts,
        "validation": {
            "status": validation.get("status"),
            "files": len(mr_context.get("changed_files") or []),
            "additions": mr_context.get("total_additions", 0),
            "deletions": mr_context.get("total_deletions", 0),
            "diff_size": mr_context.get("total_diff_size", 0),
            "checks": [{"name": check.get("name"), "status": check.get("status")} for check in validation.get("checks", [])],
        },
        "changes": compact_changes,
        "instruction": "Return compact JSON only with summary, change_type, risk, findings, testing, breaking_changes, and reviewer_attention. Use only evidence in this payload. Do not invent intent; say intent is not explicitly stated when needed. Findings must be actionable, not style-only or line-change commentary. Return an empty findings list when no actionable issue exists. Include review_scope=partial when files are excluded.",
    }
    return payload, bool(excluded), excluded


def _is_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    return "/test" in normalized or name.startswith("test_") or name.endswith("_test.py")


def review_fingerprint(project_id: Any, mr_iid: Any, payload: Dict[str, Any]) -> str:
    material = {
        "project_id": project_id,
        "mr_iid": mr_iid,
        "review": payload,
        "review_policy_version": REVIEW_POLICY_VERSION,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def deduplicate_findings(findings: Iterable[Any]) -> List[Any]:
    result = []
    seen = set()
    for finding in findings:
        if isinstance(finding, dict):
            key = (str(finding.get("issue") or finding.get("message") or finding), str(finding.get("file") or finding.get("path") or ""))
        else:
            key = (str(finding), "")
        if key not in seen:
            seen.add(key)
            result.append(finding)
    return result