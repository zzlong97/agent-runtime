import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest


def run_on_psycopg_compatible_loop(coroutine):
    """使用 psycopg 在 Windows 下兼容的事件循环执行协程。"""

    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coroutine)


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_TESTS") != "1",
        reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
    ),
]


def _manifest(
    capability_id="general_chat",
    *,
    state_scope="session",
    state_schema_version="v1",
    compatible=(),
):
    from agent_runtime.capabilities.manifest import CapabilityManifest

    return CapabilityManifest.model_validate(
        {
            "manifest_schema_version": 1,
            "capability_id": capability_id,
            "name": "测试能力",
            "description": "用于验证 Stage 3 Task 与 State Scope。",
            "enabled": True,
            "entrypoint": "agent_runtime.capabilities.general_chat.adapter:factory",
            "version": "1.0.0",
            "state_scope": state_scope,
            "state_schema_version": state_schema_version,
            "compatible_state_schema_versions": list(compatible),
            "allow_degraded": False,
            "concurrency": {
                "mode": "unlimited",
                "max_concurrency": None,
                "acquire_timeout_seconds": 1.0,
            },
            "execution": {
                "timeout_seconds": 30.0,
                "cancel_grace_seconds": 1.0,
            },
            "recovery_policy": "automatic",
            "side_effect_policy": "none",
        }
    )


def _submission(session_id, now):
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.models import RunSubmission

    payload = {"message": {"content": "S3-05 集成测试输入"}}
    return RunSubmission(
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


class EmptyCheckpointStore:
    def __init__(self):
        self.deleted = []

    async def has_checkpoint(self, thread_id):
        return False

    async def delete_thread(self, thread_id):
        self.deleted.append(thread_id)


def test_invocation_open_invariant_is_serialized_by_postgres_run_lock() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.event_models import RuntimeEvent
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
        RuntimeEventPersistenceError,
    )
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s3-05-invocation-{uuid4()}",
        _env_file=None,
    )
    now = datetime(2026, 10, 8, 13, 0, tzinfo=UTC)
    session_id = uuid4()
    submission = _submission(session_id, now)
    session_repository = PostgresSessionRepository(settings)
    run_repository = PostgresRunRepository(settings)
    first_repository = PostgresRuntimeEventRepository(settings)
    second_repository = PostgresRuntimeEventRepository(settings)

    async def exercise() -> None:
        await session_repository.setup()
        await run_repository.setup()
        await first_repository.setup()
        await session_repository.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="Invocation 并发",
                created_at=now,
                updated_at=now,
            )
        )
        await run_repository.create_or_get(submission)
        try:
            first_block, second_block = await asyncio.gather(
                first_repository.reserve_sequence_block(
                    run_id=submission.run_id,
                    block_size=1,
                ),
                second_repository.reserve_sequence_block(
                    run_id=submission.run_id,
                    block_size=1,
                ),
            )
            first_id = uuid4()
            second_id = uuid4()

            def event(seq, invocation_id, capability_id):
                return RuntimeEvent(
                    event_id=uuid4(),
                    run_id=submission.run_id,
                    seq=seq,
                    event_type="internal.capability.invocation.started",
                    source="runtime.capability",
                    visibility="internal",
                    payload={
                        "invocation_id": str(invocation_id),
                        "capability_id": capability_id,
                        "task_id": None,
                        "requested_task_action": "new",
                        "effective_task_action": None,
                        "state_scope": "session",
                        "state_schema_version": None,
                    },
                    schema_version=1,
                    durability="durable",
                    created_at=now,
                )

            commits = await asyncio.gather(
                first_repository.begin_capability_invocation(
                    event(first_block.first, first_id, "general_chat")
                ),
                second_repository.begin_capability_invocation(
                    event(second_block.first, second_id, "en_to_zh")
                ),
            )
            assert sum(not commit.recovered for commit in commits) == 1
            assert sum(commit.recovered for commit in commits) == 1
            assert commits[0].event.event_id == commits[1].event.event_id
            open_event = await first_repository.get_open_capability_invocation(
                run_id=submission.run_id
            )
            assert open_event is not None

            wrong_terminal_block = await first_repository.reserve_sequence_block(
                run_id=submission.run_id,
                block_size=1,
            )
            with pytest.raises(RuntimeEventPersistenceError) as caught:
                await first_repository.finish_capability_invocation(
                    RuntimeEvent(
                        event_id=uuid4(),
                        run_id=submission.run_id,
                        seq=wrong_terminal_block.first,
                        event_type="internal.capability.invocation.failed",
                        source="runtime.capability",
                        visibility="internal",
                        payload={
                            "invocation_id": str(uuid4()),
                            "capability_id": open_event.payload["capability_id"],
                            "task_id": None,
                            "requested_task_action": "new",
                            "effective_task_action": None,
                            "state_scope": "session",
                            "state_schema_version": None,
                            "error_code": "CAPABILITY_EXECUTION_FAILED",
                        },
                        schema_version=1,
                        durability="durable",
                        created_at=now + timedelta(seconds=1),
                    )
                )
            assert caught.value.code == "CAPABILITY_INVOCATION_MISMATCH"
            assert await first_repository.get_open_capability_invocation(
                run_id=submission.run_id
            ) == open_event
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

    run_on_psycopg_compatible_loop(exercise())


def test_task_service_transactions_degrade_restore_and_reject_incompatible() -> None:
    from agent_runtime.capabilities.invocation_service import (
        CapabilityInvocationService,
    )
    from agent_runtime.capabilities.persistence import (
        CapabilityOperationRepository,
        CapabilityTaskContractViolationError,
        CapabilityTaskContextRepository,
        CapabilityTaskRepository,
    )
    from agent_runtime.capabilities.persistence_models import CapabilityOperation
    from agent_runtime.capabilities.state_scope import (
        CapabilityStateVersionIncompatibleError,
        ChildThreadIdFactory,
    )
    from agent_runtime.capabilities.task_service import CapabilityTaskService
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.runtime.sequencer import RunSequencer
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s3-05-task-{uuid4()}",
        _env_file=None,
    )
    now = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)
    session_id = uuid4()
    submission = _submission(session_id, now)
    session_repository = PostgresSessionRepository(settings)
    run_repository = PostgresRunRepository(settings)
    event_repository = PostgresRuntimeEventRepository(settings)
    task_repository = CapabilityTaskRepository(settings)
    context_repository = CapabilityTaskContextRepository(settings)
    operation_repository = CapabilityOperationRepository(settings)
    checkpoint_store = EmptyCheckpointStore()

    async def exercise() -> None:
        await session_repository.setup()
        await run_repository.setup()
        await event_repository.setup()
        await task_repository.setup()
        await context_repository.setup()
        await operation_repository.setup()
        await session_repository.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="Task Service 事务",
                created_at=now,
                updated_at=now,
            )
        )
        await run_repository.create_or_get(submission)
        sequencer = RunSequencer.for_run(
            run_id=submission.run_id,
            response_message_id=submission.response_message_id,
            repository=event_repository,
            block_size=16,
        )
        invocation_service = CapabilityInvocationService(sequencer=sequencer)
        task_service = CapabilityTaskService(
            repository=task_repository,
            sequencer=sequencer,
            invocation_service=invocation_service,
        )
        manifest_v1 = _manifest()
        factory = ChildThreadIdFactory()
        try:
            first_invocation = await invocation_service.start(
                capability_id="general_chat",
                task_action="new",
                state_scope="session",
                created_at=now,
            )
            first = await task_service.resolve(
                manifest=manifest_v1,
                session_id=session_id,
                run_id=submission.run_id,
                invocation_id=first_invocation.invocation_id,
                task_action="new",
                updated_at=now + timedelta(seconds=1),
            )
            repeated_first = await task_service.resolve(
                manifest=manifest_v1,
                session_id=session_id,
                run_id=submission.run_id,
                invocation_id=first_invocation.invocation_id,
                task_action="new",
                updated_at=now + timedelta(seconds=1),
            )
            assert repeated_first.task.task_id == first.task.task_id
            assert repeated_first.previous_task_id is None
            async with open_database_connection(settings) as connection:
                count_cursor = await connection.execute(
                    """
                    SELECT COUNT(*) AS task_count
                    FROM capability_tasks
                    WHERE last_run_id = %s AND last_invocation_id = %s
                    """,
                    (submission.run_id, first_invocation.invocation_id),
                )
                assert (await count_cursor.fetchone())["task_count"] == 1
            await invocation_service.complete(
                invocation_id=first_invocation.invocation_id,
                capability_id="general_chat",
                task_id=first.task.task_id,
                requested_task_action=first.requested_action,
                effective_task_action=first.resolved_action,
                state_scope=first.state_scope,
                state_schema_version=first.task.state_schema_version,
                outcome="completed",
                created_at=now + timedelta(seconds=2),
            )

            second_invocation = await invocation_service.start(
                capability_id="general_chat",
                task_action="new",
                state_scope="session",
                created_at=now + timedelta(seconds=3),
            )
            second = await task_service.resolve(
                manifest=manifest_v1,
                session_id=session_id,
                run_id=submission.run_id,
                invocation_id=second_invocation.invocation_id,
                task_action="new",
                updated_at=now + timedelta(seconds=4),
            )
            assert second.previous_task_id == first.task.task_id
            second_thread = factory.build_for_manifest(
                manifest=manifest_v1,
                task=second.task,
                run_id=submission.run_id,
                invocation_id=second_invocation.invocation_id,
            )
            await task_service.rollback_rejected_new(
                resolution=second,
                run_id=submission.run_id,
                invocation_id=second_invocation.invocation_id,
                thread_id=second_thread,
                public_output_emitted=False,
                checkpoint_store=checkpoint_store,
                updated_at=now + timedelta(seconds=5),
            )
            assert await task_repository.get(second.task.task_id) is None
            restored = await context_repository.get(
                session_id=session_id,
                capability_id="general_chat",
            )
            assert restored is not None
            assert restored.current_task_id == first.task.task_id
            assert checkpoint_store.deleted == [second_thread]
            assert await event_repository.get_open_capability_invocation(
                run_id=submission.run_id
            ) is None

            third_invocation = await invocation_service.start(
                capability_id="general_chat",
                task_action="continue",
                state_scope="session",
                created_at=now + timedelta(seconds=6),
            )
            with pytest.raises(CapabilityStateVersionIncompatibleError):
                await task_service.resolve(
                    manifest=_manifest(
                        state_schema_version="v3",
                        compatible=(),
                    ),
                    session_id=session_id,
                    run_id=submission.run_id,
                    invocation_id=third_invocation.invocation_id,
                    task_action="continue",
                    updated_at=now + timedelta(seconds=7),
                )
            unchanged = await task_repository.get(first.task.task_id)
            unchanged_context = await context_repository.get(
                session_id=session_id,
                capability_id="general_chat",
            )
            assert unchanged is not None
            assert unchanged.state_schema_version == "v1"
            assert unchanged.last_invocation_id == first_invocation.invocation_id
            assert unchanged_context is not None
            assert unchanged_context.current_task_id == first.task.task_id
            await invocation_service.fail(
                invocation_id=third_invocation.invocation_id,
                capability_id="general_chat",
                task_id=None,
                requested_task_action="continue",
                effective_task_action=None,
                state_scope="session",
                state_schema_version=None,
                error_code="CAPABILITY_STATE_VERSION_INCOMPATIBLE",
                created_at=now + timedelta(seconds=8),
            )

            compatible_invocation = await invocation_service.start(
                capability_id="general_chat",
                task_action="continue",
                state_scope="session",
                created_at=now + timedelta(seconds=9),
            )
            continued = await task_service.resolve(
                manifest=_manifest(
                    state_schema_version="v3",
                    compatible=("v1",),
                ),
                session_id=session_id,
                run_id=submission.run_id,
                invocation_id=compatible_invocation.invocation_id,
                task_action="continue",
                updated_at=now + timedelta(seconds=10),
            )
            assert continued.task.task_id == first.task.task_id
            assert continued.resolved_action == "continue"
            assert continued.provisional is False
            await invocation_service.complete(
                invocation_id=compatible_invocation.invocation_id,
                capability_id="general_chat",
                task_id=continued.task.task_id,
                requested_task_action=continued.requested_action,
                effective_task_action=continued.resolved_action,
                state_scope=continued.state_scope,
                state_schema_version=continued.task.state_schema_version,
                outcome="completed",
                created_at=now + timedelta(seconds=11),
            )

            fourth_invocation = await invocation_service.start(
                capability_id="en_to_zh",
                task_action="continue",
                state_scope="invocation",
                created_at=now + timedelta(seconds=12),
            )
            degraded = await task_service.resolve(
                manifest=_manifest("en_to_zh", state_scope="invocation"),
                session_id=session_id,
                run_id=submission.run_id,
                invocation_id=fourth_invocation.invocation_id,
                task_action="continue",
                updated_at=now + timedelta(seconds=13),
            )
            assert degraded.requested_action == "continue"
            assert degraded.resolved_action == "new"
            async with open_database_connection(settings) as connection:
                cursor = await connection.execute(
                    """
                    SELECT event_type
                    FROM runtime_events
                    WHERE run_id = %s
                      AND event_type = 'internal.capability.task.continue_degraded_to_new'
                    """,
                    (submission.run_id,),
                )
                assert len(await cursor.fetchall()) == 1
            completed_degraded_task = await task_service.apply_transition(
                task=degraded.task,
                task_transition="completed",
                run_id=submission.run_id,
                invocation_id=fourth_invocation.invocation_id,
                updated_at=now + timedelta(seconds=14),
            )
            assert completed_degraded_task.status == "completed"
            repeated_completed_task = await task_service.apply_transition(
                task=degraded.task,
                task_transition="completed",
                run_id=submission.run_id,
                invocation_id=fourth_invocation.invocation_id,
                updated_at=now + timedelta(seconds=14),
            )
            assert repeated_completed_task == completed_degraded_task
            assert await context_repository.get(
                session_id=session_id,
                capability_id="en_to_zh",
            ) is None
            await invocation_service.complete(
                invocation_id=fourth_invocation.invocation_id,
                capability_id="en_to_zh",
                task_id=degraded.task.task_id,
                requested_task_action=degraded.requested_action,
                effective_task_action=degraded.resolved_action,
                state_scope=degraded.state_scope,
                state_schema_version=degraded.task.state_schema_version,
                outcome="completed",
                created_at=now + timedelta(seconds=15),
            )
            async with open_database_connection(settings) as connection:
                cursor = await connection.execute(
                    """
                    SELECT payload
                    FROM runtime_events
                    WHERE run_id = %s
                      AND event_type = 'internal.capability.invocation.completed'
                      AND payload ->> 'invocation_id' = %s
                    """,
                    (submission.run_id, str(fourth_invocation.invocation_id)),
                )
                invocation_event_row = await cursor.fetchone()
            assert invocation_event_row is not None
            assert invocation_event_row["payload"] == {
                "invocation_id": str(fourth_invocation.invocation_id),
                "capability_id": "en_to_zh",
                "task_id": str(degraded.task.task_id),
                "requested_task_action": "continue",
                "effective_task_action": "new",
                "state_scope": "invocation",
                "state_schema_version": "v1",
                "outcome": "completed",
            }

            fifth_invocation = await invocation_service.start(
                capability_id="general_chat",
                task_action="new",
                state_scope="session",
                created_at=now + timedelta(seconds=16),
            )
            fifth = await task_service.resolve(
                manifest=manifest_v1,
                session_id=session_id,
                run_id=submission.run_id,
                invocation_id=fifth_invocation.invocation_id,
                task_action="new",
                updated_at=now + timedelta(seconds=17),
            )
            await operation_repository.create_or_get(
                CapabilityOperation(
                    operation_id=uuid4(),
                    run_id=submission.run_id,
                    invocation_id=fifth_invocation.invocation_id,
                    task_id=fifth.task.task_id,
                    capability_id="general_chat",
                    operation_key="test-operation",
                    idempotency_key="a" * 64,
                    status="pending",
                    created_at=now + timedelta(seconds=18),
                    updated_at=now + timedelta(seconds=18),
                )
            )
            with pytest.raises(CapabilityTaskContractViolationError) as caught:
                await task_service.rollback_rejected_new(
                    resolution=fifth,
                    run_id=submission.run_id,
                    invocation_id=fifth_invocation.invocation_id,
                    thread_id="capability:v1:operation-violation",
                    public_output_emitted=False,
                    checkpoint_store=checkpoint_store,
                    updated_at=now + timedelta(seconds=19),
                )
            assert caught.value.code == "CAPABILITY_EXECUTION_FAILED"
            preserved = await task_repository.get(fifth.task.task_id)
            assert preserved is not None
            assert preserved.status == "active"
            preserved_context = await context_repository.get(
                session_id=session_id,
                capability_id="general_chat",
            )
            assert preserved_context is not None
            assert preserved_context.current_task_id == fifth.task.task_id
            assert await event_repository.get_open_capability_invocation(
                run_id=submission.run_id
            ) is None
        finally:
            async with open_database_connection(settings) as connection:
                await connection.execute(
                    "DELETE FROM capability_task_contexts WHERE session_id = %s",
                    (session_id,),
                )
                await connection.execute(
                    "DELETE FROM capability_operations WHERE run_id = %s",
                    (submission.run_id,),
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

    run_on_psycopg_compatible_loop(exercise())


def test_real_checkpointer_obeys_three_state_scope_thread_keys() -> None:
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from agent_runtime.capabilities.persistence_models import CapabilityTask
    from agent_runtime.capabilities.state_scope import ChildThreadIdFactory
    from agent_runtime.core.config import Settings

    settings = Settings()
    factory = ChildThreadIdFactory()
    session_id = uuid4()
    run_id = uuid4()
    other_run_id = uuid4()
    invocation_id = uuid4()
    other_invocation_id = uuid4()
    task_id = uuid4()
    other_task_id = uuid4()
    common = {
        "session_id": session_id,
        "capability_id": "general_chat",
        "state_schema_version": "v1",
    }
    invocation_thread = factory.build(
        state_scope="invocation",
        invocation_id=invocation_id,
        **common,
    )
    other_invocation_thread = factory.build(
        state_scope="invocation",
        invocation_id=other_invocation_id,
        **common,
    )
    run_thread = factory.build(state_scope="run", run_id=run_id, **common)
    same_run_thread = factory.build(
        state_scope="run",
        run_id=run_id,
        invocation_id=other_invocation_id,
        **common,
    )
    other_run_thread = factory.build(
        state_scope="run",
        run_id=other_run_id,
        **common,
    )
    task_thread = factory.build(
        state_scope="session",
        task_id=task_id,
        run_id=run_id,
        **common,
    )
    same_task_thread = factory.build(
        state_scope="session",
        task_id=task_id,
        run_id=other_run_id,
        **common,
    )
    other_task_thread = factory.build(
        state_scope="session",
        task_id=other_task_id,
        **common,
    )
    legacy_task = CapabilityTask(
        task_id=uuid4(),
        session_id=session_id,
        capability_id="general_chat",
        state_schema_version="v1",
        status="active",
        last_run_id=run_id,
        last_invocation_id=invocation_id,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        ended_at=None,
    )
    upgraded_manifest = _manifest(
        state_scope="session",
        state_schema_version="v2",
        compatible=("v1",),
    )
    legacy_thread_first_run = factory.build_for_manifest(
        manifest=upgraded_manifest,
        task=legacy_task,
        run_id=run_id,
        invocation_id=invocation_id,
    )
    legacy_thread_next_run = factory.build_for_manifest(
        manifest=upgraded_manifest,
        task=legacy_task,
        run_id=other_run_id,
        invocation_id=other_invocation_id,
    )

    def config(thread_id):
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
            }
        }

    async def exercise() -> None:
        assert invocation_thread != other_invocation_thread
        assert run_thread == same_run_thread
        assert run_thread != other_run_thread
        assert task_thread == same_task_thread
        assert task_thread != other_task_thread
        assert legacy_thread_first_run == legacy_thread_next_run
        assert "schema:v1" in legacy_thread_next_run
        assert "schema:v2" not in legacy_thread_next_run
        written = [
            invocation_thread,
            run_thread,
            task_thread,
            legacy_thread_first_run,
        ]
        cleanup = list(
            {
                invocation_thread,
                other_invocation_thread,
                run_thread,
                other_run_thread,
                task_thread,
                other_task_thread,
                legacy_thread_first_run,
                legacy_thread_next_run,
            }
        )
        async with AsyncPostgresSaver.from_conn_string(
            settings.database_connection_string
        ) as checkpointer:
            await checkpointer.setup()
            try:
                for index, thread_id in enumerate(written, start=1):
                    checkpoint = empty_checkpoint()
                    version = checkpointer.get_next_version(None, None)
                    checkpoint["channel_values"]["marker"] = index
                    checkpoint["channel_versions"]["marker"] = version
                    checkpoint["updated_channels"] = ["marker"]
                    await checkpointer.aput(
                        config(thread_id),
                        checkpoint,
                        {"source": "input", "step": -1, "parents": {}},
                        {"marker": version},
                    )

                assert await checkpointer.aget_tuple(
                    config(other_invocation_thread)
                ) is None
                assert await checkpointer.aget_tuple(config(same_run_thread)) is not None
                assert await checkpointer.aget_tuple(config(other_run_thread)) is None
                assert await checkpointer.aget_tuple(config(same_task_thread)) is not None
                assert await checkpointer.aget_tuple(config(other_task_thread)) is None
                assert await checkpointer.aget_tuple(
                    config(legacy_thread_next_run)
                ) is not None
            finally:
                for thread_id in cleanup:
                    await checkpointer.adelete_thread(thread_id)

    run_on_psycopg_compatible_loop(exercise())
