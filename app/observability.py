import json
import logging
import threading
import time
from typing import Any, Dict, Iterable, Optional

from app.ai.token_budget import redact_sensitive_text

logger = logging.getLogger(__name__)


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = {}
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, list[float]] = {}

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            self._histograms.setdefault(name, []).append(value)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": {name: list(values) for name, values in self._histograms.items()},
            }


registry = MetricsRegistry()


def record_metric(name: str, amount: int = 1) -> None:
    registry.increment(name, amount)


def set_gauge(name: str, value: float) -> None:
    registry.set_gauge(name, value)


def observe_metric(name: str, value: float) -> None:
    registry.observe(name, value)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in ("token", "secret", "password", "authorization", "cookie", "key", "private", "pat"))


def sanitize_for_log(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                cleaned[str(key)] = "[REDACTED]"
            else:
                cleaned[str(key)] = sanitize_for_log(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_for_log(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_for_log(item) for item in value]
    if isinstance(value, str):
        redacted = redact_sensitive_text(value)
        if len(redacted) > 2000:
            return redacted[:2000] + "..."
        return redacted
    return value


def log_event(event: str, level: str = "INFO", **fields: Any) -> None:
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level.upper(),
        "event": event,
        "service": "gitlab-ai-agent",
        "environment": "prod",
    }
    payload.update(sanitize_for_log(fields))
    logger.info(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def metric_snapshot() -> Dict[str, Any]:
    return registry.snapshot()


def reset_metrics() -> None:
    with registry._lock:
        registry._counters.clear()
        registry._gauges.clear()
        registry._histograms.clear()
