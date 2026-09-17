import os
from typing import Any, Dict, List


class ValidationEngine:
    """Deterministic validation engine for GitLab MR changes."""

    def __init__(self, config: Dict[str, Any] | None = None) -> None:
        self.config = {
            "max_changed_files": 20,
            "allowed_extensions": [".py", ".js", ".ts", ".json", ".yaml", ".yml", ".md"],
            "blocked_files": [".env", ".env.*", "*.pem", "*.key"],
            "max_diff_size": 100000,
            "max_additions": 500,
            "max_deletions": 500,
            "sensitive_patterns": ["BEGIN PRIVATE KEY", "AKIA", "sk_live", "xoxb-", r"password\s*="],
            **(config or {}),
        }

    def validate(self, mr_context: Dict[str, Any]) -> Dict[str, Any]:
        changed_files = mr_context.get("changed_files", [])
        checks: List[Dict[str, Any]] = []
        status = "pass"

        file_count = len(changed_files)
        file_limit = self.config["max_changed_files"]
        if file_count > file_limit:
            status = "fail"
            checks.append({
                "name": "file_count",
                "status": "fail",
                "details": f"{file_count}/{file_limit} files changed",
            })
        else:
            checks.append({
                "name": "file_count",
                "status": "pass",
                "details": f"{file_count}/{file_limit} files changed",
            })

        blocked = self._find_blocked_files(changed_files)
        if blocked:
            status = "fail"
            checks.append({
                "name": "blocked_files",
                "status": "fail",
                "details": "; ".join(blocked),
            })
        else:
            checks.append({
                "name": "blocked_files",
                "status": "pass",
                "details": "No blocked files detected",
            })

        extension_issues = self._find_extension_issues(changed_files)
        if extension_issues:
            status = "fail"
            checks.append({
                "name": "extension_check",
                "status": "fail",
                "details": "; ".join(extension_issues),
            })
        else:
            checks.append({
                "name": "extension_check",
                "status": "pass",
                "details": "Allowed file extensions only",
            })

        diff_size = int(mr_context.get("total_diff_size", 0) or 0)
        if diff_size > self.config["max_diff_size"]:
            status = "fail"
            checks.append({
                "name": "diff_size",
                "status": "fail",
                "details": f"Diff size {diff_size} exceeds limit {self.config['max_diff_size']}",
            })
        else:
            checks.append({
                "name": "diff_size",
                "status": "pass",
                "details": f"Diff size {diff_size} within limit",
            })

        total_additions = int(mr_context.get("total_additions", 0) or 0)
        total_deletions = int(mr_context.get("total_deletions", 0) or 0)
        if total_additions > self.config["max_additions"] or total_deletions > self.config["max_deletions"]:
            status = "fail"
            checks.append({
                "name": "size_limits",
                "status": "fail",
                "details": f"Additions={total_additions}, deletions={total_deletions}",
            })
        else:
            checks.append({
                "name": "size_limits",
                "status": "pass",
                "details": f"Additions={total_additions}, deletions={total_deletions}",
            })

        for changed in changed_files:
            file_path = str(changed.get("path", ""))
            if not file_path:
                continue
            if any(pattern in file_path for pattern in [".env", "key", "pem", "secret"]):
                status = "fail"
                checks.append({
                    "name": "sensitive_file",
                    "status": "fail",
                    "details": file_path,
                })
                break
        else:
            checks.append({
                "name": "sensitive_file",
                "status": "pass",
                "details": "No sensitive file names detected",
            })

        return {"status": status, "checks": checks}

    def _find_blocked_files(self, changed_files: List[Dict[str, Any]]) -> List[str]:
        blocked = self.config["blocked_files"]
        results: List[str] = []
        for changed in changed_files:
            path = str(changed.get("path", ""))
            if not path:
                continue
            normalized = os.path.basename(path)
            if any(self._matches_pattern(path, pattern) or self._matches_pattern(normalized, pattern) for pattern in blocked):
                results.append(path)
        return results

    def _find_extension_issues(self, changed_files: List[Dict[str, Any]]) -> List[str]:
        allowed = {ext.lower() for ext in self.config["allowed_extensions"]}
        issues: List[str] = []
        for changed in changed_files:
            path = str(changed.get("path", ""))
            if not path:
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext and ext not in allowed:
                issues.append(f"{path} ({ext})")
        return issues

    @staticmethod
    def _matches_pattern(value: str, pattern: str) -> bool:
        if pattern.startswith("*."):
            suffix = pattern[1:]
            return value.endswith(suffix)
        if pattern.endswith(".*"):
            prefix = pattern[:-1]
            return value.startswith(prefix)
        return value == pattern
