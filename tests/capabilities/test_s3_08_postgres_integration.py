"""S3-08 并发准入失败的真实 PostgreSQL Invocation 事件验证。"""

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_TESTS") != "1",
        reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
    ),
]


def _run_on_psycopg_compatible_loop(coroutine):
    """使用 psycopg 在 Windows 下兼容的事件循环执行协程。"""

    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coroutine)


def _manifest():
    """构造启用 session State 且无副作用的 S3-08 测试 Manifest。"""

    from agent_runtime.capabilities.manifest import CapabilityManifest

    return CapabilityManifest.model_validate(
        {
            "manifest_schema_version": 1,
            "capability_id": "timeout_cap",
            "name": "超时测试能力",
            "description": "用于验证超时后的 Invocation 与 Task 状态。",
            "enabled": True,
            "entrypoint": "agent_runtime.capabilities.fake:factory",
            "version": "1.0.0",
            "state_scope": "session",
            "state_schema_version": "v1",
            "compatible_state_schema_versions": [],
            "allow_degraded": False,
            "concurrency": {
                "mode": "unlimited",
                "max_concurrency": None,
                "acquire_timeout_seconds": 1.0,
            },
            "execution": {
                "timeout_seconds": 0.01,
                "cancel_grace_seconds": 0.01,
            },
            "recovery_policy": "automatic",
            "side_effect_policy": "none",
        }
    )


def test_busy_admission_persists_started_and_failed_without_creating_task() -> None:
    from agent_runtime.capabilities.contracts import CapabilityError
    from agent_runtime.capabilities.execution import (
        CapabilityConcurrencyController,
    )
    from agent_runtime.capabilities.invocation_service import (
        CapabilityInvocationService,
    )
    from agent_runtime.capabilities.manifest import ConcurrencyPolicy
    from agent_runtime.capabilities.persistence import CapabilityTaskRepository
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.models import RunSubmission
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.runtime.sequencer import RunSequencer
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s3-08-busy-{uuid4()}",
        _env_file=None,
    )
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    session_id = uuid4()
    payload = {"message": {"content": "S3-08 并发准入测试输入"}}
    submission = RunSubmission(
        run_id=uuid4(),
        request_id=uuid4(),
        session_id=session_id,
        thread_id=str(session_id),
        parent_run_id=None,
        run_type="normal",
        input_message_id=uuid4(),
        response_message_id=uuid4(),
        start_checkpoint_id=None,
        input_payload=payload,
        request_fingerprint=build_request_fingerprint(
            run_type="normal",
            session_id=session_id,
            request_payload=payload,
        ),
        created_at=now,
    )
    session_repository = PostgresSessionRepository(settings)
    run_repository = PostgresRunRepository(settings)
    event_repository = PostgresRuntimeEventRepository(settings)
    task_repository = CapabilityTaskRepository(settings)
    controller = CapabilityConcurrencyController(
        capability_id="busy_cap",
        policy=ConcurrencyPolicy(
            mode="bounded",
            max_concurrency=1,
            acquire_timeout_seconds=0.01,
        ),
    )

    async def scenario() -> None:
        await session_repository.setup()
        await run_repository.setup()
        await event_repository.setup()
        await task_repository.setup()
        await session_repository.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="S3-08 并发准入",
                created_at=now,
                updated_at=now,
            )
        )
        await run_repository.create_or_get(submission)
        sequencer = RunSequencer.for_run(
            run_id=submission.run_id,
            response_message_id=submission.response_message_id,
            repository=event_repository,
            block_size=4,
        )
        invocation_service = CapabilityInvocationService(sequencer=sequencer)
        try:
            async with controller.acquire():
                invocation = await invocation_service.start(
                    capability_id="busy_cap",
                    task_action="new",
                    state_scope="session",
                    created_at=now,
                )
                with pytest.raises(CapabilityError) as caught:
                    async with controller.acquire():
                        raise AssertionError("busy 准入不得进入 Task 解析")
                await invocation_service.fail(
                    invocation_id=invocation.invocation_id,
                    capability_id=invocation.capability_id,
                    task_id=None,
                    requested_task_action=invocation.task_action,
                    effective_task_action=None,
                    state_scope=invocation.state_scope,
                    state_schema_version=None,
                    error_code=caught.value.code,
                    created_at=now,
                )

            async with open_database_connection(settings) as connection:
                event_cursor = await connection.execute(
                    """
                    SELECT event_type, payload
                    FROM runtime_events
                    WHERE run_id = %s
                      AND event_type LIKE 'internal.capability.invocation.%%'
                    ORDER BY seq ASC
                    """,
                    (submission.run_id,),
                )
                events = await event_cursor.fetchall()
                task_cursor = await connection.execute(
                    """
                    SELECT count(*) AS task_count
                    FROM capability_tasks
                    WHERE last_run_id = %s
                    """,
                    (submission.run_id,),
                )
                task_row = await task_cursor.fetchone()

            assert [row["event_type"] for row in events] == [
                "internal.capability.invocation.started",
                "internal.capability.invocation.failed",
            ]
            assert events[-1]["payload"]["error_code"] == "CAPABILITY_BUSY"
            assert events[-1]["payload"]["task_id"] is None
            assert task_row is not None
            assert task_row["task_count"] == 0
        finally:
            async with open_database_connection(settings) as connection:
                await connection.execute(
                    "DELETE FROM runtime_events WHERE run_id = %s",
                    (submission.run_id,),
                )
                await connection.execute(
                    "DELETE FROM runs WHERE run_id = %s",
                    (submission.run_id,),
                )
                await connection.execute(
                    "DELETE FROM sessions WHERE session_id = %s",
                    (session_id,),
                )
                await connection.commit()

    _run_on_psycopg_compatible_loop(scenario())


def test_execution_timeout_keeps_resolved_task_active_and_maps_safe_message() -> None:
    from agent_runtime.capabilities.contracts import CapabilityError
    from agent_runtime.capabilities.execution import (
        CapabilityErrorMapper,
        CapabilityExecutionController,
    )
    from agent_runtime.capabilities.invocation_service import (
        CapabilityInvocationService,
    )
    from agent_runtime.capabilities.persistence import CapabilityTaskRepository
    from agent_runtime.capabilities.task_service import CapabilityTaskService
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )
    from agent_runtime.runtime.event_schemas import (
        MessageFinalizedPayload,
        RunCancelledPayload,
        RunCompletedPayload,
        RunFailedPayload,
        RunStartedPayload,
    )
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.models import RunSubmission
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.runtime.sequencer import RunSequencer
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s3-08-timeout-{uuid4()}",
        _env_file=None,
    )
    now = datetime(2026, 10, 9, 12, 30, tzinfo=UTC)
    session_id = uuid4()
    payload = {"message": {"content": "S3-08 执行超时测试输入"}}
    submission = RunSubmission(
        run_id=uuid4(),
        request_id=uuid4(),
        session_id=session_id,
        thread_id=str(session_id),
        parent_run_id=None,
        run_type="normal",
        input_message_id=uuid4(),
        response_message_id=uuid4(),
        start_checkpoint_id=None,
        input_payload=payload,
        request_fingerprint=build_request_fingerprint(
            run_type="normal",
            session_id=session_id,
            request_payload=payload,
        ),
        created_at=now,
    )
    manifest = _manifest()
    session_repository = PostgresSessionRepository(settings)
    run_repository = PostgresRunRepository(settings)
    event_repository = PostgresRuntimeEventRepository(settings)
    task_repository = CapabilityTaskRepository(settings)

    async def scenario() -> None:
        await session_repository.setup()
        await run_repository.setup()
        await event_repository.setup()
        await task_repository.setup()
        await session_repository.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="S3-08 执行超时",
                created_at=now,
                updated_at=now,
            )
        )
        await run_repository.create_or_get(submission)
        sequencer = RunSequencer.for_run(
            run_id=submission.run_id,
            response_message_id=submission.response_message_id,
            repository=event_repository,
            block_size=8,
        )
        invocation_service = CapabilityInvocationService(sequencer=sequencer)
        task_service = CapabilityTaskService(
            repository=task_repository,
            sequencer=sequencer,
            invocation_service=invocation_service,
        )
        try:
            await sequencer.transition_run(
                target_status="running",
                event=RuntimeEventDraft(
                    event_type="run.started",
                    source="runtime.executor",
                    visibility="public",
                    payload=RunStartedPayload(status="running"),
                    schema_version=1,
                    durability="durable",
                    created_at=now,
                ),
                updated_at=now,
            )
            invocation = await invocation_service.start(
                capability_id=manifest.capability_id,
                task_action="new",
                state_scope=manifest.state_scope,
                created_at=now,
            )
            resolution = await task_service.resolve(
                manifest=manifest,
                session_id=session_id,
                run_id=submission.run_id,
                invocation_id=invocation.invocation_id,
                task_action="new",
                updated_at=now,
            )
            controller = CapabilityExecutionController(
                capability_id=manifest.capability_id,
                policy=manifest.execution,
            )

            async def never_finishes(_cancellation):
                await asyncio.Event().wait()

            with pytest.raises(CapabilityError) as caught:
                await controller.execute(never_finishes)
            await invocation_service.fail(
                invocation_id=invocation.invocation_id,
                capability_id=manifest.capability_id,
                task_id=resolution.task.task_id,
                requested_task_action=resolution.requested_action,
                effective_task_action=resolution.resolved_action,
                state_scope=resolution.state_scope,
                state_schema_version=resolution.task.state_schema_version,
                error_code=caught.value.code,
                created_at=now,
            )

            stored_task = await task_repository.get(resolution.task.task_id)
            mapped = CapabilityErrorMapper().map(caught.value)
            assert stored_task is not None
            assert stored_task.status == "active"
            assert stored_task.ended_at is None
            assert mapped.code == "CAPABILITY_TIMEOUT"
            assert mapped.message.strip()

            await sequencer.emit(
                RuntimeEventDraft(
                    event_type="message.finalized",
                    source="runtime.executor",
                    visibility="public",
                    payload=MessageFinalizedPayload(
                        response_message_id=submission.response_message_id,
                        runtime_status="incomplete",
                        capability_id=manifest.capability_id,
                    ),
                    schema_version=1,
                    durability="durable",
                    created_at=now,
                )
            )

            terminal_results = await asyncio.gather(
                sequencer.transition_run(
                    target_status="failed",
                    event=RuntimeEventDraft(
                        event_type="run.failed",
                        source="runtime.executor",
                        visibility="public",
                        payload=RunFailedPayload(
                            status="failed",
                            code=mapped.code,
                            message=mapped.message,
                            retryable=mapped.retryable,
                        ),
                        schema_version=1,
                        durability="durable",
                        created_at=now,
                    ),
                    updated_at=now,
                    error_code=mapped.code,
                    error_message=mapped.message,
                ),
                sequencer.transition_run(
                    target_status="cancelled",
                    event=RuntimeEventDraft(
                        event_type="run.cancelled",
                        source="runtime.executor",
                        visibility="public",
                        payload=RunCancelledPayload(status="cancelled"),
                        schema_version=1,
                        durability="durable",
                        created_at=now,
                    ),
                    updated_at=now,
                ),
                sequencer.transition_run(
                    target_status="completed",
                    event=RuntimeEventDraft(
                        event_type="run.completed",
                        source="runtime.executor",
                        visibility="public",
                        payload=RunCompletedPayload(status="completed"),
                        schema_version=1,
                        durability="durable",
                        created_at=now,
                    ),
                    updated_at=now,
                ),
                return_exceptions=True,
            )
            assert sum(
                not isinstance(result, BaseException)
                for result in terminal_results
            ) == 1

            public_events = await event_repository.list_public(
                run_id=submission.run_id,
                after_seq=0,
                limit=100,
            )
            public_event_types = [event.event_type for event in public_events]
            terminal_types = [
                event_type
                for event_type in public_event_types
                if event_type in {"run.failed", "run.cancelled", "run.completed"}
            ]
            assert len(terminal_types) == 1
            assert public_event_types.index("message.finalized") < (
                public_event_types.index(terminal_types[0])
            )

            async with open_database_connection(settings) as connection:
                cursor = await connection.execute(
                    """
                    SELECT event_type, payload
                    FROM runtime_events
                    WHERE run_id = %s
                      AND event_type = 'internal.capability.invocation.failed'
                    """,
                    (submission.run_id,),
                )
                rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0]["payload"]["error_code"] == "CAPABILITY_TIMEOUT"
        finally:
            async with open_database_connection(settings) as connection:
                await connection.execute(
                    "DELETE FROM capability_task_contexts WHERE session_id = %s",
                    (session_id,),
                )
                await connection.execute(
                    "DELETE FROM capability_tasks WHERE session_id = %s",
                    (session_id,),
                )
                await connection.execute(
                    "DELETE FROM runtime_events WHERE run_id = %s",
                    (submission.run_id,),
                )
                await connection.execute(
                    "DELETE FROM runs WHERE run_id = %s",
                    (submission.run_id,),
                )
                await connection.execute(
                    "DELETE FROM sessions WHERE session_id = %s",
                    (session_id,),
                )
                await connection.commit()

    _run_on_psycopg_compatible_loop(scenario())
