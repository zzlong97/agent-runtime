import asyncio
from contextlib import asynccontextmanager


def test_application_lifespan_opens_and_closes_production_chat_service(
    monkeypatch,
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
    app = main.create_app(Settings(_env_file=None))

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            assert app.state.chat_service is service
            assert events == ["open"]

    asyncio.run(exercise())

    assert events == ["open", "close"]
    assert not hasattr(app.state, "chat_service")


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
