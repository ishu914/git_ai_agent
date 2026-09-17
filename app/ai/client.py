import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class OpenRouterClient:
    """Thin OpenRouter client using the OpenAI-compatible API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: float = 30,
        max_retries: int = 2,
    ) -> None:
        settings = get_settings()
        resolved_api_key = api_key or settings.openrouter_api_key
        resolved_model = model or settings.openrouter_model

        if not resolved_api_key:
            raise ValueError("OPENROUTER_API_KEY is required but was not provided.")

        self.api_key = resolved_api_key
        self.model = resolved_model
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries

    def build_chat_payload(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        return payload

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost",
            "X-Title": "gitlab-ai-agent",
        }

    def _request(self, method: str, path: str, json_body: Optional[Dict[str, Any]] = None) -> httpx.Response:
        final_url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                response = httpx.request(
                    method=method,
                    url=final_url,
                    headers=self._headers(),
                    json=json_body,
                    timeout=self.timeout,
                )
                if response.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                    logger.warning(
                        "OpenRouter retry %s/%s after status %s",
                        attempt + 1,
                        self.max_retries,
                        response.status_code,
                    )
                    continue
                return response
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt < self.max_retries:
                    logger.warning("OpenRouter timeout on attempt %s/%s", attempt + 1, self.max_retries)
                    continue
                raise
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self.max_retries:
                    logger.warning("OpenRouter HTTP error on attempt %s/%s: %s", attempt + 1, self.max_retries, exc)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("OpenRouter request failed without an error detail.")

    def chat_completion(self, messages: List[Dict[str, Any]], temperature: float = 0.2, max_tokens: Optional[int] = None) -> Dict[str, Any]:
        payload = self.build_chat_payload(messages=messages, temperature=temperature, max_tokens=max_tokens)
        response = self._request("POST", "/chat/completions", payload)

        if response.status_code >= 400:
            logger.error("OpenRouter API error: %s - %s", response.status_code, response.text[:500])
            raise RuntimeError(f"OpenRouter API request failed with status {response.status_code}: {response.text[:500]}")

        try:
            content = response.json()
        except ValueError as exc:
            raise ValueError("OpenRouter response was not valid JSON") from exc

        if "choices" not in content or not content["choices"]:
            raise ValueError("OpenRouter response did not include any choices.")

        message = content["choices"][0].get("message", {})
        if not message:
            raise ValueError("OpenRouter response did not include a message payload.")

        raw_text = message.get("content")
        if not raw_text:
            raise ValueError("OpenRouter returned an empty content payload.")

        try:
            if isinstance(raw_text, str):
                return json.loads(raw_text)
            return raw_text
        except json.JSONDecodeError as exc:
            raise ValueError("OpenRouter returned malformed JSON content for the request.") from exc

    def health_check(self) -> Dict[str, Any]:
        """Lightweight connectivity test without exposing secrets."""
        try:
            response = self._request("GET", "/models")
            if response.status_code >= 400:
                return {"status": "error", "http_status": response.status_code, "message": "openrouter_unavailable"}
            return {"status": "ok", "http_status": response.status_code, "message": "openrouter_reachable"}
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("OpenRouter health check failed")
            return {"status": "error", "message": str(exc)}
