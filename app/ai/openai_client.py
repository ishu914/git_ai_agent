"""OpenAI-compatible client used for OpenAI and compatible gateways."""

from typing import Dict, Optional

from app.ai.client import OpenRouterClient


class OpenAIClient(OpenRouterClient):
    """OpenAI chat-completions client sharing the established response parser."""

    def __init__(self, api_key: str, model: str, base_url: str, max_retries: int = 0) -> None:
        super().__init__(api_key=api_key, model=model, base_url=base_url, max_retries=max_retries)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
