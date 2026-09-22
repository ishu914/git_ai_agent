from contextlib import asynccontextmanager
from fastapi import FastAPI, Response, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.api.webhook import router as webhook_router
from app.config import get_settings
from app.events.manager import get_event_manager
from app.observability import metric_snapshot


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: recover stale processing events and start dispatcher loop
    event_manager = get_event_manager()
    await event_manager.start()
    yield
    # Shutdown: stop scheduling tasks and gracefully drain active MR jobs
    await event_manager.shutdown()


def create_app() -> FastAPI:
    settings = get_settings()
    settings.validate_required_runtime()
    app = FastAPI(
        title=settings.app_name.replace("-", " ").title(),
        version="0.1.0",
        description="Central GitLab AI Agent service for self-hosted GitLab.",
        lifespan=lifespan,
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
        """Indicates whether the application process is alive. Fast response, no external AI calls."""
        return {
            "status": "healthy",
            "service": "gitlab-ai-agent",
            "processing_architecture": "single_process_durable",
        }

    @app.get("/ready")
    async def readiness(response: Response) -> dict:
        """Detailed readiness check for storage, config, and provider availability."""
        settings = get_settings()
        errors = []

        # 1. Event Store Check
        storage_status = "ok"
        try:
            from app.events.store import EventStore
            store = EventStore(settings.database_path)
            counts = store.status_counts()
        except Exception as exc:
            storage_status = "unavailable"
            errors.append(f"event_store_error: {exc}")

        # 2. AI Provider Candidates Check
        from app.ai.orchestrator import AIOrchestrator
        orchestrator = AIOrchestrator()
        try:
            candidates = orchestrator.candidates()
            candidate_count = len(candidates)
        except Exception as exc:
            candidates = []
            candidate_count = 0
            errors.append(f"ai_discovery_error: {exc}")

        ai_status = "ok" if candidate_count > 0 else "degraded"

        # Determine readiness HTTP status
        is_ready = storage_status == "ok"
        if not is_ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {
                "status": "not_ready",
                "event_store": storage_status,
                "ai_providers": ai_status,
                "available_candidates": candidate_count,
                "errors": errors,
            }

        return {
            "status": "ready",
            "event_store": storage_status,
            "ai_providers": ai_status,
            "available_candidates": candidate_count,
            "processing_model": "single_process_durable",
        }

    @app.get("/metrics")
    async def metrics() -> dict:
        return metric_snapshot()

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
