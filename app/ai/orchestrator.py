import logging
import time
from typing import Any, Dict, List, Optional

from app.ai.client import (
    OpenRouterAPIError,
    OpenRouterClient,
    OpenRouterIncompleteResponseError,
    OpenRouterResponseError,
)
from app.ai.groq_client import GroqClient
from app.ai.types import AIModelCandidate, AIUnavailableError
from app.config import get_settings

logger = logging.getLogger(__name__)


class AIOrchestrator:
    """Bounded provider/model fallback for structured AI requests."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.cooldowns: Dict[str, float] = {}
        self.last_failure_reason = "AI providers unavailable."

    def _available(self, candidate: AIModelCandidate) -> bool:
        return self.cooldowns.get(f"{candidate.provider}:{candidate.model_id}", 0) <= time.monotonic()

    def _cooldown(self, candidate: AIModelCandidate) -> None:
        key = f"{candidate.provider}:{candidate.model_id}"
        self.cooldowns[key] = time.monotonic() + self.settings.ai_model_cooldown_seconds

    def _openrouter_candidates(self) -> List[AIModelCandidate]:
        if not self.settings.openrouter_enabled or not self.settings.openrouter_api_key:
            return []
        client = OpenRouterClient(api_key=self.settings.openrouter_api_key, model=self.settings.openrouter_model, max_retries=0)
        if self.settings.openrouter_model_strategy == "dynamic-free":
            candidates = client.discover_models(self.settings.openrouter_model_discovery_cache_ttl_seconds)
            if candidates:
                return candidates[: self.settings.openrouter_free_model_max_attempts]
        return [AIModelCandidate("openrouter", self.settings.openrouter_model, supports_json=True)]

    def _groq_candidates(self) -> List[AIModelCandidate]:
        if not self.settings.groq_enabled or not self.settings.groq_api_key:
            return []
        client = GroqClient(api_key=self.settings.groq_api_key, model=self.settings.openrouter_model, max_retries=0)
        candidates = client.discover_models(self.settings.groq_model_discovery_cache_ttl_seconds)
        return candidates[: self.settings.groq_model_max_attempts]

    def candidates(self) -> List[AIModelCandidate]:
        return self._openrouter_candidates() + self._groq_candidates()

    def _client_for(self, candidate: AIModelCandidate):
        if candidate.provider == "openrouter":
            return OpenRouterClient(api_key=self.settings.openrouter_api_key, model=candidate.model_id, max_retries=0)
        return GroqClient(api_key=self.settings.groq_api_key, model=candidate.model_id, max_retries=0)

    @staticmethod
    def _failure_reason(error: Exception) -> str:
        message = str(error).lower()
        if isinstance(error, OpenRouterIncompleteResponseError) or "incomplete" in message or "finish_reason=length" in message:
            return "incomplete response"
        if "429" in message or "rate" in message or "quota" in message:
            return "rate limit or quota exhausted"
        if isinstance(error, (TimeoutError,)) or "timeout" in message:
            return "provider timeout"
        if "5" in message and "status" in message:
            return "provider unavailable"
        if isinstance(error, OpenRouterResponseError):
            return "invalid structured response"
        if isinstance(error, OpenRouterAPIError):
            return "provider returned an error"
        return "provider unavailable"

    def chat_completion(self, messages: List[Dict[str, Any]], temperature: float = 0.2, max_tokens: Optional[int] = None) -> Dict[str, Any]:
        candidates = [candidate for candidate in self.candidates() if candidate.enabled and self._available(candidate)]
        if not candidates:
            self.last_failure_reason = "All configured AI providers/models are unavailable."
            raise AIUnavailableError(self.last_failure_reason)

        attempts = 0
        for candidate in candidates:
            if attempts >= self.settings.ai_total_max_attempts:
                break
            attempts += 1
            logger.info("AI request: provider=%s model=%s", candidate.provider, candidate.model_id)
            try:
                result = self._client_for(candidate).chat_completion(messages, temperature, max_tokens)
                logger.info("AI success: provider=%s model=%s", candidate.provider, candidate.model_id)
                return result
            except Exception as exc:
                self._cooldown(candidate)
                self.last_failure_reason = self._failure_reason(exc)
                logger.warning(
                    "AI fallback: provider=%s model=%s error=%s",
                    candidate.provider,
                    candidate.model_id,
                    self.last_failure_reason,
                )

        raise AIUnavailableError(f"All configured AI providers/models failed: {self.last_failure_reason}")

    def inventory(self) -> Dict[str, Any]:
        openrouter = self._openrouter_candidates()
        groq = self._groq_candidates()
        return {
            "openrouter_configured": bool(self.settings.openrouter_api_key),
            "openrouter": [candidate.__dict__ for candidate in openrouter],
            "groq_configured": bool(self.settings.groq_api_key),
            "groq": [candidate.__dict__ for candidate in groq],
        }
