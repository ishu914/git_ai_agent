from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.api.webhook import router as webhook_router
from app.config import get_settings
from app.observability import metric_snapshot, set_gauge
from app.worker.store import JobStore


def create_app() -> FastAPI:
    settings = get_settings()
    settings.validate_required_runtime()
    app = FastAPI(
        title=settings.app_name.replace("-", " ").title(),
        version="0.1.0",
        description="Central GitLab AI Agent service for self-hosted GitLab.",
    )
    app.include_router(webhook_router)

    class SecurityHeadersMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "no-store"
            return response

    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "healthy",
            "service": "gitlab-ai-agent",
        }

    @app.get("/ready")
    async def readiness() -> dict:
        try:
            JobStore(settings.worker_database_path).initialize()
            storage_status = "ready"
        except Exception:
            storage_status = "unavailable"
        return {
            "status": "ready" if storage_status == "ready" else "not_ready",
            "storage": storage_status,
            "worker_enabled": settings.worker_enabled,
        }

    @app.get("/metrics")
    async def metrics() -> dict:
        snapshot = metric_snapshot()
        try:
            counts = JobStore(settings.worker_database_path).status_counts()
            for status_name, value in counts.items():
                snapshot.setdefault("gauges", {})[f"{status_name.lower()}_jobs"] = value
        except Exception:
            pass
        return snapshot

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.ai_agent_host,
        port=settings.ai_agent_port,
        reload=False,
    )
