import os
from typing import Any, Dict, Optional

import yaml


def load_project_config(project_name: str) -> Dict[str, Any]:
    config_dir = os.path.join(os.getcwd(), "config", "projects")
    default_file = os.path.join(config_dir, "default.yaml")
    project_file = os.path.join(config_dir, f"{project_name}.yaml")

    config: Dict[str, Any] = {}
    for path in [default_file, project_file]:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
                if isinstance(loaded, dict):
                    config.update(loaded)

    return {
        "max_changed_files": 20,
        "allowed_extensions": [".py", ".js", ".ts", ".json", ".yaml", ".yml", ".md"],
        "blocked_files": [".env", ".env.*", "*.pem", "*.key"],
        "ai": {"external_providers_allowed": True},
        "external_providers_allowed": True,
        **config,
    }


def get_project_settings(project_path: Optional[str] = None, project_name: Optional[str] = None) -> Dict[str, Any]:
    if project_path:
        resolved = project_path.replace("/", "-")
        return load_project_config(resolved)
    if project_name:
        return load_project_config(project_name)
    return load_project_config("default")
