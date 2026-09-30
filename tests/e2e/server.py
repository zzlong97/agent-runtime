"""为 Playwright 启动 PostgreSQL + Fake Model 的真实 ASGI 服务。"""

import asyncio
import os

import uvicorn

from agent_runtime.chat import ChatService, open_chat_service
from agent_runtime.core.config import Settings
from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
from agent_runtime.main import create_app

async def _delete_e2e_sessions(service: ChatService) -> None:
    """清理浏览器验收专用用户遗留的 Session 及关联持久化数据。"""

    while True:
        page = await service.list_sessions(cursor=None, limit=100)
        if not page.items:
            return
        for session in page.items:
            await service.delete_session(session_id=session.session_id)


async def _serve() -> None:
    """显式启用后启动服务，并在前后清理专用测试数据。"""

    if os.getenv("RUN_POSTGRES_TESTS") != "1":
        raise RuntimeError(
            "浏览器端到端验收必须显式设置 RUN_POSTGRES_TESTS=1"
        )
    run_id = os.getenv("PLAYWRIGHT_E2E_RUN_ID")
    port_value = os.getenv("PLAYWRIGHT_E2E_PORT")
    if not run_id or not port_value:
        raise RuntimeError("浏览器端到端验收缺少运行标识或独立服务端口")
    try:
        port = int(port_value)
    except ValueError as error:
        raise RuntimeError("浏览器端到端验收端口必须是整数") from error
    if port < 1024 or port > 65_535:
        raise RuntimeError("浏览器端到端验收端口必须介于 1024 至 65535")
    settings = Settings(
        local_user_id=f"stage-two-playwright-{run_id}",
        dashscope_api_key=None,
        llm_base_url=None,
        llm_model=None,
        runtime_demo_mode=True,
        run_cancel_grace_seconds=0.1,
    )
    async with open_chat_service(settings) as service:
        await _delete_e2e_sessions(service)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(settings, chat_service=service),
                host="127.0.0.1",
                port=port,
                log_level="warning",
            )
        )
        try:
            await server.serve()
        finally:
            await _delete_e2e_sessions(service)


if __name__ == "__main__":
    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(_serve())
