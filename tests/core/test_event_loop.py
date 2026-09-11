import asyncio


def test_database_event_loop_factory_uses_selector_on_windows(
    monkeypatch,
) -> None:
    from agent_runtime.core import event_loop

    monkeypatch.setattr(event_loop.sys, "platform", "win32")

    loop = event_loop.psycopg_compatible_loop_factory()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()
