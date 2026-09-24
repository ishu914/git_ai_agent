"""Anthropic Messages API client, also usable with Anthropic-compatible gateways."""

from typing import Any, Dict, List, Optional

from app.ai.client import OpenRouterAPIError, OpenRouterClient, OpenRouterResponseError, parse_json_content


class AnthropicClient(OpenRouterClient):
    def __init__(self, api_key: str, model: str, base_url: str, max_retries: int = 0) -> None:
        super().__init__(api_key=api_key, model=model, base_url=base_url, max_retries=max_retries)

    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def chat_completion(
        self, messages: List[Dict[str, Any]], temperature: float = 0.2, max_tokens: Optional[int] = None
    ) -> Dict[str, Any]:
        system = "\n".join(str(message["content"]) for message in messages if message.get("role") == "system")
        request_messages = [
            {"role": message.get("role", "user"), "content": message.get("content", "")}
            for message in messages if message.get("role") != "system"
        ]
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": request_messages,
            "system": system,
            "temperature": temperature,
            "max_tokens": max_tokens or 1200,
        }
        response = self._request("POST", "/v1/messages", payload)
        if response.status_code >= 400:
            raise OpenRouterAPIError(f"Anthropic API error: status={response.status_code}")
        content = response.json()
        blocks = content.get("content") if isinstance(content, dict) else None
        text = blocks[0].get("text") if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict) else None
        if not text:
            raise OpenRouterResponseError("Anthropic returned an empty content payload.")
        usage = content.get("usage") if isinstance(content, dict) else None
        if isinstance(usage, dict):
            self.last_usage = {
                "prompt_tokens": int(usage.get("input_tokens", 0)),
                "completion_tokens": int(usage.get("output_tokens", 0)),
                "total_tokens": int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)),
            }
        return parse_json_content(text)
