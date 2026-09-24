"""Ollama native chat API client for locally hosted models."""

from typing import Any, Dict, List, Optional

from app.ai.client import OpenRouterAPIError, OpenRouterClient, OpenRouterResponseError, parse_json_content


class OllamaClient(OpenRouterClient):
    def __init__(self, model: str, base_url: str, max_retries: int = 0) -> None:
        super().__init__(api_key="ollama-local", model=model, base_url=base_url, max_retries=max_retries)

    def _headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json"}

    def chat_completion(
        self, messages: List[Dict[str, Any]], temperature: float = 0.2, max_tokens: Optional[int] = None
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {"temperature": temperature},
        }
        if max_tokens is not None:
            payload["options"]["num_predict"] = max_tokens
        response = self._request("POST", "/api/chat", payload)
        if response.status_code >= 400:
            raise OpenRouterAPIError(f"Ollama API error: status={response.status_code}")
        content = response.json()
        message = content.get("message") if isinstance(content, dict) else None
        text = message.get("content") if isinstance(message, dict) else None
        if not text:
            raise OpenRouterResponseError("Ollama returned an empty content payload.")
        return parse_json_content(text)
