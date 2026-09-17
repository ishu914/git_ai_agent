import pytest

from app.gitlab.client import GitLabClient


def test_gitlab_client_requires_token(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    with pytest.raises(ValueError, match="GITLAB_TOKEN"):
        GitLabClient(token=None)


def test_gitlab_client_builds_base_url_and_headers(monkeypatch):
    monkeypatch.setenv("GITLAB_URL", "http://192.168.2.86")
    monkeypatch.setenv("GITLAB_TOKEN", "demo-token")

    client = GitLabClient()

    assert client.base_url == "http://192.168.2.86"
    assert client.token == "demo-token"
    assert client._headers()["PRIVATE-TOKEN"] == "demo-token"


def test_gitlab_client_path_methods():
    client = GitLabClient(base_url="http://example.com", token="demo-token")

    assert client.get_project.__name__ == "get_project"
    assert client.get_merge_request.__name__ == "get_merge_request"
    assert client.get_merge_request_changes.__name__ == "get_merge_request_changes"
    assert client.get_merge_request_commits.__name__ == "get_merge_request_commits"
