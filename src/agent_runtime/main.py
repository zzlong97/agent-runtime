"""FastAPI application entry point."""

from fastapi import FastAPI

from agent_runtime import __version__
from agent_runtime.api.routes.health import router as health_router
from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the AgentRuntime ASGI application."""

    runtime_settings = settings or get_settings()
    configure_logging(runtime_settings.log_level)

    application = FastAPI(title="AgentRuntime", version=__version__)
    application.include_router(health_router)
    return application


app = create_app()
