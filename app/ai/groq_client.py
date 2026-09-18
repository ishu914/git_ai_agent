from typing import Any, Dict, List, Optional

from app.ai.client import OpenRouterClient
from app.ai.model_discovery import CACHE, candidates_from_catalog
from app.ai.types import AIModelCandidate
from app.config import get_settings


class GroqClient(OpenRouterClient):
    """OpenAI-compatible Groq client with the same safe response parsing."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, max_retries: int = 0) -> None:
        settings = get_settings()
        super().__init__(
            api_key=api_key or settings.groq_api_key,
            model=model or "",
            base_url=settings.groq_base_url,
            max_retries=max_retries,
        )

    def discover_models(self, ttl_seconds: int) -> List[AIModelCandidate]:
        return CACHE.get_or_fetch("groq", ttl_seconds, self._fetch_models)

    def _fetch_models(self) -> List[AIModelCandidate]:
        response = self._request("GET", "/models")
        if response.status_code >= 400:
            return []
        return candidates_from_catalog("groq", response.json(), free_only=False)
