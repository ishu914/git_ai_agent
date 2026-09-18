import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from app.ai.types import AIModelCandidate

logger = logging.getLogger(__name__)


class ModelDiscoveryCache:
    def __init__(self) -> None:
        self._entries: Dict[str, tuple[float, List[AIModelCandidate]]] = {}
        self._lock = threading.Lock()

    def get_or_fetch(
        self,
        key: str,
        ttl_seconds: int,
        fetcher: Callable[[], List[AIModelCandidate]],
    ) -> List[AIModelCandidate]:
        now = time.monotonic()
        with self._lock:
            cached = self._entries.get(key)
            if cached and now - cached[0] < ttl_seconds:
                return list(cached[1])
        try:
            models = fetcher()
        except Exception:
            logger.warning("AI model discovery failed: provider=%s", key)
            return list(cached[1]) if cached else []
        with self._lock:
            self._entries[key] = (now, list(models))
        return models


CACHE = ModelDiscoveryCache()


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _supports_json(model: Dict[str, Any]) -> bool:
    parameters = model.get("supported_parameters") or model.get("architecture", {}).get("supported_parameters") or []
    return "response_format" in parameters or "structured_outputs" in parameters


def _supports_chat(provider: str, model: Dict[str, Any]) -> bool:
    if provider != "groq":
        return True
    model_id = str(model.get("id", "")).lower()
    excluded_terms = ("whisper", "speech", "audio", "embedding", "moderation", "safety", "guard")
    if any(term in model_id for term in excluded_terms):
        return False
    modalities = model.get("architecture", {})
    input_modalities = modalities.get("input_modalities") if isinstance(modalities, dict) else None
    output_modalities = modalities.get("output_modalities") if isinstance(modalities, dict) else None
    if input_modalities and not any(str(item).lower() in {"text", "image"} for item in input_modalities):
        return False
    if output_modalities and "text" not in {str(item).lower() for item in output_modalities}:
        return False
    return True


def candidates_from_catalog(
    provider: str,
    catalog: Any,
    free_only: bool = False,
) -> List[AIModelCandidate]:
    if not isinstance(catalog, dict) or not isinstance(catalog.get("data"), list):
        return []
    candidates: List[AIModelCandidate] = []
    for index, model in enumerate(catalog["data"]):
        if not isinstance(model, dict) or not isinstance(model.get("id"), str):
            continue
        model_id = model["id"]
        if not _supports_chat(provider, model):
            continue
        pricing = model.get("pricing") if isinstance(model.get("pricing"), dict) else {}
        is_free = model_id.endswith(":free") or all(str(pricing.get(key, "")) in {"0", "0.0", "0.00"} for key in ("prompt", "completion"))
        if free_only and not is_free:
            continue
        candidates.append(
            AIModelCandidate(
                provider=provider,
                model_id=model_id,
                priority=index,
                supports_json=_supports_json(model),
                supports_code_review=True,
                context_length=_as_int(model.get("context_length")),
                pricing=pricing,
            )
        )
    return sorted(candidates, key=lambda item: (not item.supports_json, item.priority, item.model_id))
