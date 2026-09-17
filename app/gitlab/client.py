import logging
from typing import Any, Dict, List, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class GitLabClient:
    """Reusable GitLab API client for self-hosted GitLab."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = 30,
        max_retries: int = 2,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.gitlab_url).rstrip("/")
        self.token = token or settings.gitlab_token
        self.timeout = timeout
        self.max_retries = max_retries

        if not self.token:
            raise ValueError("GITLAB_TOKEN is required but was not provided.")

    def _headers(self) -> Dict[str, str]:
        return {
            "PRIVATE-TOKEN": self.token,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None) -> httpx.Response:
        url = f"{self.base_url}/api/v4{path}"
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                response = httpx.request(
                    method=method,
                    url=url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
                )
                if response.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                    logger.warning(
                        "GitLab retry %s/%s after status %s",
                        attempt + 1,
                        self.max_retries,
                        response.status_code,
                    )
                    continue
                return response
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt < self.max_retries:
                    logger.warning("GitLab timeout on attempt %s/%s", attempt + 1, self.max_retries)
                    continue
                raise
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self.max_retries:
                    logger.warning("GitLab HTTP error on attempt %s/%s: %s", attempt + 1, self.max_retries, exc)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("GitLab request failed without an error detail.")

    def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        response = self._request("GET", path, params=params)
        if response.status_code >= 400:
            raise RuntimeError(f"GitLab API error {response.status_code}: {response.text[:500]}")
        try:
            return response.json()
        except ValueError as exc:
            raise ValueError("GitLab API returned invalid JSON") from exc

    def get_project(self, project_id: str) -> Dict[str, Any]:
        return self._get_json(f"/projects/{project_id}")

    def get_merge_request(self, project_id: str, mr_iid: int) -> Dict[str, Any]:
        return self._get_json(f"/projects/{project_id}/merge_requests/{mr_iid}")

    def get_merge_request_changes(self, project_id: str, mr_iid: int) -> Dict[str, Any]:
        return self._get_json(f"/projects/{project_id}/merge_requests/{mr_iid}/changes")

    def get_merge_request_commits(self, project_id: str, mr_iid: int) -> List[Dict[str, Any]]:
        return self._get_json(f"/projects/{project_id}/merge_requests/{mr_iid}/commits")

    def get_merge_request_approvals(self, project_id: str, mr_iid: int) -> Dict[str, Any]:
        return self._get_json(f"/projects/{project_id}/merge_requests/{mr_iid}/approvals")

    def update_merge_request_description(self, project_id: str, mr_iid: int, description: str) -> Dict[str, Any]:
        response = self._request(
            "PUT",
            f"/projects/{project_id}/merge_requests/{mr_iid}",
            json_body={"description": description},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"GitLab MR description update failed: {response.text[:500]}")
        return response.json()

    def create_merge_request_note(self, project_id: str, mr_iid: int, body: str) -> Dict[str, Any]:
        response = self._request(
            "POST",
            f"/projects/{project_id}/merge_requests/{mr_iid}/notes",
            json_body={"body": body},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"GitLab note creation failed: {response.text[:500]}")
        return response.json()

    def get_merge_request_notes(self, project_id: str, mr_iid: int) -> List[Dict[str, Any]]:
        return self._get_json(f"/projects/{project_id}/merge_requests/{mr_iid}/notes", params={"per_page": 100})

    def update_merge_request_note(self, project_id: str, mr_iid: int, note_id: int, body: str) -> Dict[str, Any]:
        response = self._request(
            "PUT",
            f"/projects/{project_id}/merge_requests/{mr_iid}/notes/{note_id}",
            json_body={"body": body},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"GitLab note update failed: {response.text[:500]}")
        return response.json()

    def get_file_contents(self, project_id: str, file_path: str, ref: str = "HEAD") -> str:
        encoded_path = file_path.replace("/", "%2F")
        params = {"ref": ref}
        response = self._request("GET", f"/projects/{project_id}/repository/files/{encoded_path}", params=params)
        if response.status_code >= 400:
            raise RuntimeError(f"GitLab file read failed: {response.text[:500]}")
        payload = response.json()
        import base64

        return base64.b64decode(payload["content"]).decode("utf-8")

    def get_project_files(self, project_id: str, path: str = "", ref: str = "HEAD") -> List[Dict[str, Any]]:
        params = {"ref": ref, "path": path}
        return self._get_json(f"/projects/{project_id}/repository/tree", params=params)
