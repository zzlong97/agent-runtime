"""Stage 2 单实例 Active Run Registry 测试。"""

import asyncio
from uuid import UUID

import pytest


SESSION_ID = UUID("00000000-0000-0000-0000-000000001201")
OTHER_SESSION_ID = UUID("00000000-0000-0000-0000-000000001202")
MESSAGE_ID = UUID("00000000-0000-0000-0000-000000001203")


def test_registry_rejects_second_run_for_same_session_before_release() -> None:
    from agent_runtime.runs import ActiveRunRegistry, SessionBusyError

    async def exercise() -> None:
        registry = ActiveRunRegistry()
        first = await registry.reserve(SESSION_ID, MESSAGE_ID)

        with pytest.raises(SessionBusyError) as captured:
            await registry.reserve(SESSION_ID, MESSAGE_ID)

        assert captured.value.code == "SESSION_BUSY"
        assert captured.value.status_code == 409
        assert captured.value.retryable is True
        await registry.release(first)

    asyncio.run(exercise())


def test_registry_allows_different_sessions_and_ignores_stale_release() -> None:
    from agent_runtime.runs import ActiveRunRegistry, SessionBusyError

    async def exercise() -> None:
        registry = ActiveRunRegistry()
        first = await registry.reserve(SESSION_ID, MESSAGE_ID)
        other = await registry.reserve(OTHER_SESSION_ID, MESSAGE_ID)
        await registry.release(first)

        replacement = await registry.reserve(SESSION_ID, MESSAGE_ID)
        await registry.release(first)

        with pytest.raises(SessionBusyError):
            await registry.reserve(SESSION_ID, MESSAGE_ID)

        await registry.release(replacement)
        await registry.release(other)

    asyncio.run(exercise())


def test_stale_response_cancel_does_not_cancel_replacement_run() -> None:
    from agent_runtime.runs import ActiveRunRegistry

    async def exercise() -> None:
        registry = ActiveRunRegistry()
        stale_run = await registry.reserve(SESSION_ID, MESSAGE_ID)
        await registry.release(stale_run)

        replacement = await registry.reserve(SESSION_ID, MESSAGE_ID)
        cancelled = await registry.request_cancel(stale_run, "disconnected")

        assert cancelled is False
        assert replacement.cancel_reason is None
        assert replacement.terminal_future.done() is False
        await registry.release(replacement)

    asyncio.run(exercise())


def test_cancelled_attached_producer_releases_run_and_unblocks_waiter() -> None:
    from agent_runtime.runs import ActiveRunRegistry

    async def exercise() -> None:
        registry = ActiveRunRegistry()
        run = await registry.reserve(SESSION_ID, MESSAGE_ID)
        producer_started = asyncio.Event()

        async def producer() -> None:
            try:
                producer_started.set()
                await asyncio.Event().wait()
            finally:
                await registry.release(run)

        task = asyncio.create_task(producer())
        await registry.attach_producer(run, task)
        await producer_started.wait()

        cancelled = await registry.request_cancel(run, "stopped")

        assert cancelled is True
        assert run.cancel_reason == "stopped"
        assert task.cancelled()
        replacement = await registry.reserve(SESSION_ID, MESSAGE_ID)
        await registry.release(replacement)

    asyncio.run(exercise())


def test_cancel_before_producer_attachment_finishes_reservation() -> None:
    from agent_runtime.runs import ActiveRunRegistry

    async def exercise() -> None:
        registry = ActiveRunRegistry()
        run = await registry.reserve(SESSION_ID, MESSAGE_ID)

        cancelled = await registry.request_cancel(run, "disconnected")

        assert cancelled is True
        assert run.cancel_reason == "disconnected"
        assert run.terminal_future.done()
        assert await run.event_queue.get() is None
        replacement = await registry.reserve(SESSION_ID, MESSAGE_ID)
        await registry.release(replacement)

    asyncio.run(exercise())


def test_concurrent_cancel_requests_do_not_interrupt_producer_cleanup() -> None:
    from agent_runtime.runs import ActiveRunRegistry

    async def exercise() -> None:
        registry = ActiveRunRegistry()
        run = await registry.reserve(SESSION_ID, MESSAGE_ID)
        producer_started = asyncio.Event()
        cleanup_started = asyncio.Event()
        allow_cleanup = asyncio.Event()

        async def producer() -> None:
            try:
                producer_started.set()
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await allow_cleanup.wait()
                await registry.release(run)

        task = asyncio.create_task(producer())
        await registry.attach_producer(run, task)
        await producer_started.wait()

        first = asyncio.create_task(
            registry.request_cancel(run, "stopped")
        )
        await cleanup_started.wait()
        second = asyncio.create_task(
            registry.request_cancel(run, "disconnected")
        )
        await asyncio.sleep(0)
        allow_cleanup.set()

        results = await asyncio.wait_for(
            asyncio.gather(first, second),
            timeout=1,
        )
        assert results == [True, True]
        assert run.cancel_reason == "stopped"
        replacement = await registry.reserve(SESSION_ID, MESSAGE_ID)
        await registry.release(replacement)

    asyncio.run(exercise())
