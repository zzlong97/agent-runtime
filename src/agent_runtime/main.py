"""FastAPI 应用入口与生产聊天资源生命周期。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
from pathlib import Path
from time import perf_counter
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_runtime import __version__
from agent_runtime.api.routes.chat import router as chat_router
from agent_runtime.api.routes.health import router as health_router
from agent_runtime.chat import open_chat_service
from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.logging import configure_logging, log_business_event


_STATIC_DIR = Path(__file__).resolve().parent / "static"
logger = logging.getLogger(__name__)


class _HttpBusinessLoggingMiddleware:
    """直接透传 ASGI send，避免隔离 SSE 对真实网络写入的超时。"""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        started_at = perf_counter()
        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))
        log_business_event(
            logger,
            "HTTP请求进入",
            method=method,
            path=path,
        )

        async def send_with_response_log(message: Message) -> None:
            await send(message)
            if message["type"] == "http.response.start":
                log_business_event(
                    logger,
                    "HTTP响应已建立",
                    method=method,
                    path=path,
                    status_code=message["status"],
                    duration_ms=round(
                        (perf_counter() - started_at) * 1000,
                        2,
                    ),
                )

        try:
            await self._app(scope, receive, send_with_response_log)
        except Exception as error:
            log_business_event(
                logger,
                "HTTP请求异常",
                level=logging.ERROR,
                method=method,
                path=path,
                error_type=type(error).__name__,
                duration_ms=round((perf_counter() - started_at) * 1000, 2),
            )
            raise


def create_app(
    settings: Settings | None = None,
    *,
    chat_service: Any | None = None,
) -> FastAPI:
    """创建 AgentRuntime ASGI 应用，并允许测试注入聊天服务。"""

    runtime_settings = settings or get_settings()
    configure_logging(
        runtime_settings.log_level,
        log_dir=runtime_settings.log_dir,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """在 ASGI 生命周期内持有三套 Checkpointer 与聊天服务。"""

        startup_started_at = perf_counter()
        log_business_event(
            logger,
            "应用启动开始",
            service_mode="injected" if chat_service is not None else "production",
        )
        if chat_service is not None:
            log_business_event(
                logger,
                "应用启动完成",
                service_mode="injected",
                duration_ms=round(
                    (perf_counter() - startup_started_at) * 1000,
                    2,
                ),
            )
            try:
                yield
            except BaseException as error:
                log_business_event(
                    logger,
                    "应用关闭失败",
                    level=logging.ERROR,
                    service_mode="injected",
                    error_type=type(error).__name__,
                )
                raise
            else:
                log_business_event(logger, "应用关闭开始", service_mode="injected")
                log_business_event(logger, "应用关闭完成", service_mode="injected")
            return
        startup_completed = False
        shutdown_started_at: float | None = None
        try:
            async with open_chat_service(runtime_settings) as production_service:
                application.state.chat_service = production_service
                startup_completed = True
                log_business_event(
                    logger,
                    "应用启动完成",
                    service_mode="production",
                    duration_ms=round(
                        (perf_counter() - startup_started_at) * 1000,
                        2,
                    ),
                )
                try:
                    yield
                finally:
                    shutdown_started_at = perf_counter()
                    log_business_event(
                        logger,
                        "应用关闭开始",
                        service_mode="production",
                    )
                    del application.state.chat_service
        except BaseException as error:
            log_business_event(
                logger,
                "应用关闭失败" if startup_completed else "应用启动失败",
                level=logging.ERROR,
                service_mode="production",
                error_type=type(error).__name__,
                duration_ms=round(
                    (
                        perf_counter()
                        - (
                            shutdown_started_at
                            if shutdown_started_at is not None
                            else startup_started_at
                        )
                    )
                    * 1000,
                    2,
                ),
            )
            raise
        else:
            log_business_event(
                logger,
                "应用关闭完成",
                service_mode="production",
                duration_ms=round(
                    (perf_counter() - shutdown_started_at) * 1000,
                    2,
                )
                if shutdown_started_at is not None
                else None,
            )

    application = FastAPI(
        title="AgentRuntime",
        version=__version__,
        lifespan=lifespan,
    )
    if chat_service is not None:
        application.state.chat_service = chat_service
    application.add_middleware(_HttpBusinessLoggingMiddleware)

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
