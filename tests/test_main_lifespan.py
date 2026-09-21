import asyncio
from contextlib import asynccontextmanager

import pytest


def test_application_lifespan_opens_and_closes_production_chat_service(
    monkeypatch,
    tmp_path,
) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime import main

    events: list[str] = []
    service = object()

    @asynccontextmanager
    async def fake_open_chat_service(settings):
        events.append("open")
        yield service
        events.append("close")

    monkeypatch.setattr(main, "open_chat_service", fake_open_chat_service)
    app = main.create_app(Settings(log_dir=tmp_path, _env_file=None))

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            assert app.state.chat_service is service
            assert events == ["open"]

    asyncio.run(exercise())

    assert events == ["open", "close"]
    assert not hasattr(app.state, "chat_service")
    log_text = next(tmp_path.glob("agent-runtime-*.log")).read_text(
        encoding="utf-8"
    )
    assert "应用启动开始" in log_text
    assert "应用启动完成" in log_text
    assert "应用关闭开始" in log_text
    assert "应用关闭完成" in log_text


def test_application_lifespan_uses_injected_chat_service_without_production_open(
    monkeypatch,
) -> None:
    from agent_runtime import main

    injected_service = object()

    @asynccontextmanager
    async def unexpected_open_chat_service(settings):
        raise AssertionError("注入聊天服务后不应打开生产依赖")
        yield

    monkeypatch.setattr(
        main,
        "open_chat_service",
        unexpected_open_chat_service,
    )
    app = main.create_app(chat_service=injected_service)

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            assert app.state.chat_service is injected_service

    asyncio.run(exercise())


def test_application_lifespan_logs_startup_failure_without_false_success(
    monkeypatch,
    tmp_path,
) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime import main

    @asynccontextmanager
    async def failing_open_chat_service(settings):
        raise RuntimeError("数据库初始化失败正文不得写入业务日志")
        yield

    monkeypatch.setattr(main, "open_chat_service", failing_open_chat_service)
    app = main.create_app(Settings(log_dir=tmp_path, _env_file=None))

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            raise AssertionError("依赖初始化失败后不应进入应用运行期")

    with pytest.raises(RuntimeError):
        asyncio.run(exercise())

    log_text = next(tmp_path.glob("agent-runtime-*.log")).read_text(
        encoding="utf-8"
    )
    assert "应用启动失败" in log_text
    assert "应用启动完成" not in log_text
    assert "应用关闭完成" not in log_text
    assert "数据库初始化失败正文不得写入业务日志" not in log_text


def test_application_lifespan_logs_shutdown_failure_without_false_success(
    monkeypatch,
    tmp_path,
) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime import main

    service = object()

    @asynccontextmanager
    async def failing_close_chat_service(settings):
        yield service
        raise RuntimeError("服务关闭失败正文不得写入业务日志")

    monkeypatch.setattr(main, "open_chat_service", failing_close_chat_service)
    app = main.create_app(Settings(log_dir=tmp_path, _env_file=None))

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            assert app.state.chat_service is service

    with pytest.raises(RuntimeError):
        asyncio.run(exercise())

    log_text = next(tmp_path.glob("agent-runtime-*.log")).read_text(
        encoding="utf-8"
    )
    assert "应用启动完成" in log_text
    assert "应用关闭开始" in log_text
    assert "应用关闭失败" in log_text
    assert "应用关闭完成" not in log_text
    assert "服务关闭失败正文不得写入业务日志" not in log_text
