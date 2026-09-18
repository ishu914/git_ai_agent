from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class AIModelCandidate:
    provider: str
    model_id: str
    priority: int = 100
    supports_json: bool = False
    supports_code_review: bool = True
    context_length: Optional[int] = None
    pricing: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class AIResponse:
    success: bool
    provider: str
    model: str
    content: Optional[Dict[str, Any]] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    finish_reason: Optional[str] = None


class AIProviderError(RuntimeError):
    def __init__(self, message: str, error_type: str = "provider_error") -> None:
        super().__init__(message)
        self.error_type = error_type


class AIUnavailableError(RuntimeError):
    pass