from app.project_config import get_project_settings, load_project_config


def test_load_project_config_reads_default_and_project_overrides(tmp_path, monkeypatch):
    config_dir = tmp_path / "config" / "projects"
    config_dir.mkdir(parents=True)
    (config_dir / "default.yaml").write_text("max_changed_files: 15\nallowed_extensions:\n  - .py\n", encoding="utf-8")
    (config_dir / "test-group-admin-rd.yaml").write_text("max_changed_files: 10\nblocked_files:\n  - .env\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)

    default_cfg = load_project_config("default")
    project_cfg = load_project_config("test-group-admin-rd")

    assert default_cfg["max_changed_files"] == 15
    assert project_cfg["max_changed_files"] == 10
    assert ".env" in project_cfg["blocked_files"]


def test_get_project_settings_uses_project_path_name():
    settings = get_project_settings(project_path="test-group/admin-rd")
    assert isinstance(settings, dict)
