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


def test_registry_blocks_new_run_during_deletion_and_tombstones_success() -> None:
    from agent_runtime.runs import (
        ActiveRunRegistry,
        SessionBusyError,
        SessionUnavailableError,
    )

    async def exercise() -> None:
        registry = ActiveRunRegistry()

        async with registry.deleting(SESSION_ID) as deletion:
            with pytest.raises(SessionBusyError) as deleting_error:
                await registry.reserve(SESSION_ID, MESSAGE_ID)
            assert deleting_error.value.code == "SESSION_BUSY"
            assert deleting_error.value.status_code == 409
            deletion.mark_deleted()

        with pytest.raises(SessionUnavailableError) as deleted_error:
            await registry.reserve(SESSION_ID, MESSAGE_ID)
        assert deleted_error.value.code == "SESSION_NOT_FOUND"
        assert deleted_error.value.status_code == 404
        assert deleted_error.value.retryable is False

        other = await registry.reserve(OTHER_SESSION_ID, MESSAGE_ID)
        await registry.release(other)

    asyncio.run(exercise())


def test_failed_deletion_reopens_session_for_retry_or_new_run() -> None:
    from agent_runtime.runs import ActiveRunRegistry

    async def exercise() -> None:
        registry = ActiveRunRegistry()

        with pytest.raises(RuntimeError):
            async with registry.deleting(SESSION_ID):
                raise RuntimeError("删除清理失败")

        run = await registry.reserve(SESSION_ID, MESSAGE_ID)
        await registry.release(run)

    asyncio.run(exercise())


def test_concurrent_deletion_guards_keep_session_blocked_between_owners() -> None:
    from agent_runtime.runs import ActiveRunRegistry, SessionBusyError

    async def exercise() -> None:
        registry = ActiveRunRegistry()
        deleting = registry.deleting
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_entered = asyncio.Event()
        release_second = asyncio.Event()

        async def first_delete() -> None:
            async with deleting(SESSION_ID):
                first_entered.set()
                await release_first.wait()

        async def second_delete() -> None:
            await first_entered.wait()
            async with deleting(SESSION_ID):
                second_entered.set()
                await release_second.wait()

        first_task = asyncio.create_task(first_delete())
        second_task = asyncio.create_task(second_delete())
        await first_entered.wait()
        await asyncio.sleep(0)

        with pytest.raises(SessionBusyError):
            await registry.reserve(SESSION_ID, MESSAGE_ID)

        release_first.set()
        await second_entered.wait()
        with pytest.raises(SessionBusyError):
            await registry.reserve(SESSION_ID, MESSAGE_ID)

        release_second.set()
        await asyncio.gather(first_task, second_task)
        run = await registry.reserve(SESSION_ID, MESSAGE_ID)
        await registry.release(run)

    asyncio.run(exercise())


def test_deletion_waits_for_existing_session_operation_to_finish() -> None:
    from agent_runtime.runs import ActiveRunRegistry

    async def exercise() -> None:
        registry = ActiveRunRegistry()
        session_operation = registry.session_operation
        operation_started = asyncio.Event()
        release_operation = asyncio.Event()
        deletion_entered = asyncio.Event()

        async def use_session() -> None:
            async with session_operation(SESSION_ID):
                operation_started.set()
                await release_operation.wait()

        async def delete_session() -> None:
            await operation_started.wait()
            async with registry.deleting(SESSION_ID):
                deletion_entered.set()

        operation_task = asyncio.create_task(use_session())
        deletion_task = asyncio.create_task(delete_session())
        await operation_started.wait()
        await asyncio.sleep(0)

        assert deletion_entered.is_set() is False
        release_operation.set()
        await asyncio.gather(operation_task, deletion_task)
        assert deletion_entered.is_set() is True

    asyncio.run(exercise())


def test_session_operation_is_rejected_after_deletion_starts() -> None:
    from agent_runtime.runs import ActiveRunRegistry, SessionUnavailableError

    async def exercise() -> None:
        registry = ActiveRunRegistry()

        async with registry.deleting(SESSION_ID) as deletion:
            with pytest.raises(SessionUnavailableError) as captured:
                async with registry.session_operation(SESSION_ID):
                    raise AssertionError("删除开始后不得进入 Session 操作")
            assert captured.value.code == "SESSION_NOT_FOUND"
            assert captured.value.status_code == 404
            deletion.mark_deleted()

        with pytest.raises(SessionUnavailableError):
            async with registry.session_operation(SESSION_ID):
                raise AssertionError("删除完成后不得进入 Session 操作")

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


def test_registry_returns_current_run_without_exposing_internal_mapping() -> None:
    from agent_runtime.runs import ActiveRunRegistry

    async def exercise() -> None:
        registry = ActiveRunRegistry()

        assert await registry.get_active(SESSION_ID) is None
        run = await registry.reserve(SESSION_ID, MESSAGE_ID)
        assert await registry.get_active(SESSION_ID) is run
        await registry.release(run)
        assert await registry.get_active(SESSION_ID) is None

    asyncio.run(exercise())


def test_cancel_does_not_reclassify_run_after_terminal_finalization_begins() -> None:
    from agent_runtime.runs import ActiveRunRegistry

    async def exercise() -> None:
        registry = ActiveRunRegistry()
        run = await registry.reserve(SESSION_ID, MESSAGE_ID)
        producer_cancelled = asyncio.Event()

        async def producer() -> None:
            try:
                await run.terminal_future
            except asyncio.CancelledError:
                producer_cancelled.set()
                raise

        task = asyncio.create_task(producer())
        await registry.attach_producer(run, task)
        assert await registry.begin_finalization(run) is True

        cancel_waiter = asyncio.create_task(
            registry.request_cancel(run, "stopped")
        )
        await asyncio.sleep(0)

        assert cancel_waiter.done() is False
        assert run.cancel_reason is None
        assert producer_cancelled.is_set() is False
        await registry.release(run)
        assert await cancel_waiter is False
        await task

    asyncio.run(exercise())


def test_producer_attachment_is_ignored_while_unstarted_run_is_cancelling() -> None:
    from agent_runtime.runs import ActiveRunRegistry

    async def exercise() -> None:
        registry = ActiveRunRegistry()
        run = await registry.reserve(SESSION_ID, MESSAGE_ID)
        finalizer_started = asyncio.Event()
        allow_finalizer = asyncio.Event()

        async def finalizer() -> None:
            finalizer_started.set()
            await allow_finalizer.wait()
            await registry.release(run)

        cancel_waiter = asyncio.create_task(
            registry.request_cancel(
                run,
                "stopped",
                unstarted_finalizer=finalizer,
            )
        )
        await finalizer_started.wait()

        late_task = asyncio.create_task(asyncio.Event().wait())
        attached = await registry.attach_producer(run, late_task)

        assert attached is False
        await asyncio.gather(late_task, return_exceptions=True)
        allow_finalizer.set()
        assert await cancel_waiter is True

    asyncio.run(exercise())
