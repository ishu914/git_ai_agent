from fastapi import FastAPI

from app.api.webhook import router as webhook_router
from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name.replace("-", " ").title(),
        version="0.1.0",
        description="Central GitLab AI Agent service for self-hosted GitLab.",
    )
    app.include_router(webhook_router)

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "healthy",
            "service": "gitlab-ai-agent",
        }

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
