"""FastAPI 应用入口与生产聊天资源生命周期。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agent_runtime import __version__
from agent_runtime.api.routes.chat import router as chat_router
from agent_runtime.api.routes.health import router as health_router
from agent_runtime.chat import open_chat_service
from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.logging import configure_logging


_STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    settings: Settings | None = None,
    *,
    chat_service: Any | None = None,
) -> FastAPI:
    """创建 AgentRuntime ASGI 应用，并允许测试注入聊天服务。"""

    runtime_settings = settings or get_settings()
    configure_logging(runtime_settings.log_level)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """在 ASGI 生命周期内持有三套 Checkpointer 与聊天服务。"""

        if chat_service is not None:
            yield
            return
        async with open_chat_service(runtime_settings) as production_service:
            application.state.chat_service = production_service
            try:
                yield
            finally:
                del application.state.chat_service

    application = FastAPI(
        title="AgentRuntime",
        version=__version__,
        lifespan=lifespan,
    )
    if chat_service is not None:
        application.state.chat_service = chat_service
    application.include_router(health_router)
    application.include_router(chat_router)
    application.mount(
        "/assets",
        StaticFiles(directory=_STATIC_DIR / "assets", check_dir=False),
        name="chat-assets",
    )

    @application.get("/chat", include_in_schema=False)
    async def chat_page() -> FileResponse:
        """返回已构建且可离线部署的聊天演示页面。"""

        return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")

    return application


app = create_app()
