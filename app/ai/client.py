import json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class OpenRouterAPIError(RuntimeError):
    """Raised when OpenRouter rejects an API request."""


class OpenRouterResponseError(ValueError):
    """Raised when OpenRouter returns an unusable completion response."""


class OpenRouterEmptyContentError(OpenRouterResponseError):
    """Raised when a completion has no message content."""


class OpenRouterMalformedJSONError(OpenRouterResponseError):
    """Raised when completion content is not valid JSON."""


class OpenRouterIncompleteResponseError(OpenRouterResponseError):
    """Raised when the model stopped before producing a complete response."""


def parse_json_content(content: Any) -> Dict[str, Any]:
    """Parse structured completion content without reconstructing malformed output."""
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content.strip():
        raise OpenRouterEmptyContentError("OpenRouter returned an empty content payload.")

    text = content.strip()
    fence_match = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        text = fence_match.group(1).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OpenRouterMalformedJSONError("OpenRouter returned malformed JSON content for the request.") from exc

    if not isinstance(parsed, dict):
        raise OpenRouterMalformedJSONError("OpenRouter returned JSON that was not an object.")
    return parsed


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

    def _safe_error_message(self, response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return "unparseable_error_response"
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str):
                    return message[:200]
            if isinstance(error, str):
                return error[:200]
        return "openrouter_request_rejected"

    def chat_completion(self, messages: List[Dict[str, Any]], temperature: float = 0.2, max_tokens: Optional[int] = None) -> Dict[str, Any]:
        payload = self.build_chat_payload(messages=messages, temperature=temperature, max_tokens=max_tokens)
        response = self._request("POST", "/chat/completions", payload)

        if response.status_code in {400, 422} and "response_format" in payload:
            logger.warning(
                "OpenRouter model rejected structured output; retrying without response_format: status=%s model=%s message=%s",
                response.status_code,
                self.model,
                self._safe_error_message(response),
            )
            fallback_payload = dict(payload)
            fallback_payload.pop("response_format", None)
            response = self._request("POST", "/chat/completions", fallback_payload)

        if response.status_code >= 400:
            error_message = self._safe_error_message(response)
            logger.error(
                "OpenRouter API error: status=%s model=%s message=%s",
                response.status_code,
                self.model,
                error_message,
            )
            raise OpenRouterAPIError(f"OpenRouter API request failed with status {response.status_code}: {error_message}")

        try:
            content = response.json()
        except ValueError as exc:
            logger.warning("OpenRouter response envelope was not JSON: model=%s status=%s", self.model, response.status_code)
            raise OpenRouterResponseError("OpenRouter response was not valid JSON") from exc

        choices = content.get("choices") if isinstance(content, dict) else None
        if not choices:
            raise OpenRouterResponseError("OpenRouter response did not include any choices.")

        choice = choices[0]
        if not isinstance(choice, dict):
            raise OpenRouterResponseError("OpenRouter response choice was not an object.")
        finish_reason = choice.get("finish_reason")
        if finish_reason in {"length", "max_tokens", "content_filter"}:
            logger.warning(
                "OpenRouter returned an incomplete response: model=%s status=%s finish_reason=%s",
                self.model,
                response.status_code,
                finish_reason,
            )
            raise OpenRouterIncompleteResponseError(
                f"OpenRouter response was incomplete (finish_reason={finish_reason})."
            )

        message = choice.get("message")
        if not message:
            raise ValueError("OpenRouter response did not include a message payload.")

        raw_text = message.get("content")
        if not raw_text:
            logger.warning(
                "OpenRouter returned empty content: model=%s status=%s finish_reason=%s",
                self.model,
                response.status_code,
                finish_reason,
            )
            raise OpenRouterEmptyContentError("OpenRouter returned an empty content payload.")

        try:
            return parse_json_content(raw_text)
        except OpenRouterResponseError:
            logger.warning(
                "OpenRouter returned unusable structured content: model=%s status=%s finish_reason=%s",
                self.model,
                response.status_code,
                finish_reason,
            )
            raise

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
