"""S2.5 单进程 Run Coordinator 行为测试。"""

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest


def _queued_run(run_number: int):
    from agent_runtime.runtime.models import Run

    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    run_id = UUID(f"00000000-0000-0000-0000-{run_number:012d}")
    session_id = UUID(f"10000000-0000-0000-0000-{run_number:012d}")
    return Run(
        run_id=run_id,
        request_id=UUID(f"20000000-0000-0000-0000-{run_number:012d}"),
        session_id=session_id,
        thread_id=str(session_id),
        parent_run_id=None,
        run_type="normal",
        input_message_id=UUID(
            f"30000000-0000-0000-0000-{run_number:012d}"
        ),
        response_message_id=UUID(
            f"40000000-0000-0000-0000-{run_number:012d}"
        ),
        start_checkpoint_id=None,
        input_payload={"message": {"content": "协调器测试"}},
        request_fingerprint="a" * 64,
        status="queued",
        recovery_attempts=0,
        seq_high_watermark=0,
        error_code=None,
        error_message=None,
        created_at=now,
        started_at=None,
        finished_at=None,
        updated_at=now,
    )


class FakeRunRepository:
    def __init__(self, runs=()) -> None:
        self.runs = {run.run_id: run for run in runs}
        self.list_calls: list[str] = []

    async def get(self, run_id):
        return self.runs[run_id]

    async def list_active_for_user(self, *, user_id):
        self.list_calls.append(user_id)
        return [
            run
            for run in self.runs.values()
            if run.status
            in {
                "queued",
                "running",
                "recovering",
                "interrupted",
                "cancel_requested",
            }
        ]


def test_coordinator_submit_wakeup_executes_one_local_task() -> None:
    from agent_runtime.runtime.coordinator import RunCoordinator

    run = _queued_run(1)
    repository = FakeRunRepository([run])
    executed: list[UUID] = []
    finished = asyncio.Event()

    async def execute(selected_run):
        executed.append(selected_run.run_id)
        repository.runs[selected_run.run_id] = replace(
            selected_run,
            status="completed",
        )
        finished.set()

    async def exercise() -> None:
        coordinator = RunCoordinator(
            repository=repository,
            user_id="local-user",
            scan_interval_seconds=60,
        )
        await coordinator.start(execute, scan_on_startup=False)
        try:
            await asyncio.gather(
                coordinator.wake(run.run_id),
                coordinator.wake(run.run_id),
            )
            await asyncio.wait_for(finished.wait(), timeout=1)
            await coordinator.wait_until_idle()
        finally:
            await coordinator.close()

    asyncio.run(exercise())
    assert executed == [run.run_id]


def test_coordinator_compensates_a_lost_submit_wakeup() -> None:
    from agent_runtime.runtime.coordinator import RunCoordinator

    run = _queued_run(2)
    repository = FakeRunRepository([run])
    finished = asyncio.Event()

    async def execute(selected_run):
        repository.runs[selected_run.run_id] = replace(
            selected_run,
            status="completed",
        )
        finished.set()

    async def exercise() -> None:
        coordinator = RunCoordinator(
            repository=repository,
            user_id="local-user",
            scan_interval_seconds=0.01,
        )
        await coordinator.start(execute, scan_on_startup=False)
        try:
            # 故意不调用 wake，由周期扫描补偿丢失的进程内通知。
            await asyncio.wait_for(finished.wait(), timeout=1)
            await coordinator.wait_until_idle()
        finally:
            await coordinator.close()

    asyncio.run(exercise())
    assert repository.list_calls


def test_coordinator_startup_scan_dispatches_queued_runs_only() -> None:
    from agent_runtime.runtime.coordinator import RunCoordinator

    queued = _queued_run(3)
    running = replace(_queued_run(4), status="running")
    repository = FakeRunRepository([queued, running])
    executed: list[UUID] = []
    finished = asyncio.Event()

    async def execute(selected_run):
        executed.append(selected_run.run_id)
        repository.runs[selected_run.run_id] = replace(
            selected_run,
            status="completed",
        )
        finished.set()

    async def exercise() -> None:
        coordinator = RunCoordinator(
            repository=repository,
            user_id="local-user",
            scan_interval_seconds=60,
        )
        await coordinator.start(execute)
        try:
            await asyncio.wait_for(finished.wait(), timeout=1)
            await coordinator.wait_until_idle()
        finally:
            await coordinator.close()

    asyncio.run(exercise())
    assert repository.list_calls == ["local-user"]
    assert executed == [queued.run_id]


def test_coordinator_close_cancels_local_tasks_for_later_recovery() -> None:
    from agent_runtime.runtime.coordinator import RunCoordinator

    run = _queued_run(5)
    repository = FakeRunRepository([run])
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def execute(selected_run):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def exercise() -> None:
        coordinator = RunCoordinator(
            repository=repository,
            user_id="local-user",
            scan_interval_seconds=60,
        )
        await coordinator.start(execute, scan_on_startup=False)
        await coordinator.wake(run.run_id)
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(coordinator.close(), timeout=1)
        assert cancelled.is_set()

    asyncio.run(exercise())


def test_coordinator_cancel_first_notifies_cooperative_executor() -> None:
    from agent_runtime.runtime.coordinator import RunCoordinator

    run = _queued_run(6)
    repository = FakeRunRepository([run])
    started = asyncio.Event()
    cooperatively_stopped = asyncio.Event()

    async def exercise() -> None:
        coordinator = RunCoordinator(
            repository=repository,
            user_id="local-user",
            scan_interval_seconds=60,
            cancel_grace_seconds=1,
        )

        async def execute(selected_run):
            started.set()
            await coordinator.wait_cancel_requested(selected_run.run_id)
            cooperatively_stopped.set()

        await coordinator.start(execute, scan_on_startup=False)
        try:
            await coordinator.wake(run.run_id)
            await asyncio.wait_for(started.wait(), timeout=1)
            await coordinator.request_cancel(run.run_id)
            await asyncio.wait_for(cooperatively_stopped.wait(), timeout=1)
            await coordinator.wait_until_idle()
        finally:
            await coordinator.close()

    asyncio.run(exercise())


def test_coordinator_force_cancels_executor_after_grace_timeout() -> None:
    from agent_runtime.runtime.coordinator import RunCoordinator

    run = _queued_run(7)
    repository = FakeRunRepository([run])
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def execute(_selected_run):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def exercise() -> None:
        coordinator = RunCoordinator(
            repository=repository,
            user_id="local-user",
            scan_interval_seconds=60,
            cancel_grace_seconds=0.01,
        )
        await coordinator.start(execute, scan_on_startup=False)
        try:
            await coordinator.wake(run.run_id)
            await asyncio.wait_for(started.wait(), timeout=1)
            await coordinator.request_cancel(run.run_id)
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            await coordinator.wait_until_idle()
        finally:
            await coordinator.close()

    asyncio.run(exercise())


def test_coordinator_does_not_interrupt_slow_cancel_finalization() -> None:
    from agent_runtime.runtime.coordinator import RunCoordinator

    run = _queued_run(9)
    repository = FakeRunRepository([run])
    started = asyncio.Event()
    finalization_started = asyncio.Event()
    release_finalization = asyncio.Event()
    finished = asyncio.Event()

    async def exercise() -> None:
        coordinator = RunCoordinator(
            repository=repository,
            user_id="local-user",
            scan_interval_seconds=60,
            cancel_grace_seconds=0.01,
        )

        async def execute(selected_run):
            started.set()
            await coordinator.wait_cancel_requested(selected_run.run_id)
            await coordinator.begin_cancel_finalization(selected_run.run_id)
            finalization_started.set()
            await release_finalization.wait()
            finished.set()

        await coordinator.start(execute, scan_on_startup=False)
        try:
            await coordinator.wake(run.run_id)
            await asyncio.wait_for(started.wait(), timeout=1)
            await coordinator.request_cancel(run.run_id)
            await asyncio.wait_for(finalization_started.wait(), timeout=1)
            await asyncio.sleep(0.03)
            assert not finished.is_set()

            waiter = asyncio.create_task(coordinator.wait_for_run(run.run_id))
            await asyncio.sleep(0)
            assert not waiter.done()
            release_finalization.set()
            await asyncio.wait_for(waiter, timeout=1)
            assert finished.is_set()
        finally:
            await coordinator.close()

    asyncio.run(exercise())


def test_cancelled_waiter_does_not_interrupt_cancel_finalization() -> None:
    """取消等待者或关闭协调器都不能取得取消终态收尾任务的所有权。"""

    from agent_runtime.runtime.coordinator import RunCoordinator

    run = _queued_run(10)
    repository = FakeRunRepository([run])
    finalization_started = asyncio.Event()
    release_finalization = asyncio.Event()
    finalization_finished = asyncio.Event()
    executor_cancelled = asyncio.Event()

    async def exercise() -> None:
        coordinator = RunCoordinator(
            repository=repository,
            user_id="local-user",
            scan_interval_seconds=60,
            cancel_grace_seconds=0.01,
        )

        async def execute(selected_run):
            try:
                await coordinator.wait_cancel_requested(selected_run.run_id)
                await coordinator.begin_cancel_finalization(selected_run.run_id)
                finalization_started.set()
                await release_finalization.wait()
                finalization_finished.set()
            except asyncio.CancelledError:
                executor_cancelled.set()
                raise

        await coordinator.start(execute, scan_on_startup=False)
        await coordinator.wake(run.run_id)
        await coordinator.request_cancel(run.run_id)
        await asyncio.wait_for(finalization_started.wait(), timeout=1)

        waiter = asyncio.create_task(coordinator.wait_for_run(run.run_id))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not executor_cancelled.is_set()

        close_task = asyncio.create_task(coordinator.close())
        await asyncio.sleep(0)
        assert not close_task.done()
        release_finalization.set()
        await asyncio.wait_for(close_task, timeout=1)
        assert finalization_finished.is_set()
        assert not executor_cancelled.is_set()

    asyncio.run(exercise())


def test_coordinator_startup_dispatches_cancel_requested_for_projection() -> None:
    from agent_runtime.runtime.coordinator import RunCoordinator

    run = replace(_queued_run(8), status="cancel_requested")
    repository = FakeRunRepository([run])
    executed: list[UUID] = []

    async def execute(selected_run):
        executed.append(selected_run.run_id)

    async def exercise() -> None:
        coordinator = RunCoordinator(
            repository=repository,
            user_id="local-user",
            scan_interval_seconds=60,
            cancel_grace_seconds=1,
        )
        await coordinator.start(execute)
        try:
            await coordinator.wait_until_idle()
        finally:
            await coordinator.close()

    asyncio.run(exercise())
    assert executed == [run.run_id]


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
)
def test_real_postgres_scan_compensates_missing_process_wakeup() -> None:
    """真实 PostgreSQL queued Run 在没有 wake 时仍会被周期扫描发现。"""

    from uuid import uuid4

    from agent_runtime.core.config import Settings
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.coordinator import RunCoordinator
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.models import RunSubmission
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-coordinator-{uuid4()}",
        _env_file=None,
    )
    now = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)
    session_id = uuid4()
    run_id = uuid4()
    repository = PostgresRunRepository(settings)
    session_repository = PostgresSessionRepository(settings)
    discovered = asyncio.Event()

    async def execute(run) -> None:
        assert run.run_id == run_id
        discovered.set()

    async def exercise() -> None:
        await session_repository.setup()
        await repository.setup()
        await session_repository.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="Coordinator 补偿",
                created_at=now,
                updated_at=now,
            )
        )
        submission = RunSubmission(
            run_id=run_id,
            request_id=uuid4(),
            session_id=session_id,
            thread_id=str(session_id),
            parent_run_id=None,
            run_type="normal",
            input_message_id=uuid4(),
            response_message_id=uuid4(),
            start_checkpoint_id=None,
            input_payload={"message": {"content": "丢失唤醒"}},
            request_fingerprint=build_request_fingerprint(
                run_type="normal",
                session_id=session_id,
                request_payload={"message": {"content": "丢失唤醒"}},
            ),
            created_at=now,
        )
        coordinator = RunCoordinator(
            repository=repository,
            user_id=settings.local_user_id,
            scan_interval_seconds=0.01,
        )
        try:
            await repository.create_or_get(submission)
            await coordinator.start(execute, scan_on_startup=False)
            # 不调用 wake，必须由真实数据库扫描补偿。
            await asyncio.wait_for(discovered.wait(), timeout=1)
        finally:
            await coordinator.close()
            async with open_database_connection(settings) as connection:
                await connection.execute(
                    "DELETE FROM runs WHERE session_id = %s",
                    (session_id,),
                )
                await connection.execute(
                    "DELETE FROM sessions WHERE session_id = %s",
                    (session_id,),
                )
                await connection.commit()

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(exercise())
