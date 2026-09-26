import asyncio
import gc
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict, Field


def run_on_psycopg_compatible_loop(coroutine):
    """使用 psycopg 在 Windows 下兼容的事件循环执行协程。"""

    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coroutine)


def _submission(session_id, now):
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.models import RunSubmission

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
        input_payload={"message": {"content": "RuntimeEvent 测试输入"}},
        request_fingerprint=build_request_fingerprint(
            run_type="normal",
            session_id=session_id,
            request_payload={"message": {"content": "RuntimeEvent 测试输入"}},
        ),
        created_at=now,
    )


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
)
def test_run_sequencer_reserves_blocks_keeps_gaps_and_filters_internal_events() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
        RuntimeEventPersistenceError,
    )
    from agent_runtime.runtime.event_schemas import (
        MessageDeltaPayload,
        MessageStartedPayload,
        RuntimeEventSchemaError,
    )
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.runtime.sequencer import RunSequencer
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    class InternalDiagnosticPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")

        diagnostic_code: str = Field(
            description="仅供服务端诊断使用的稳定内部代码。"
        )

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-event-gap-{uuid4()}",
        _env_file=None,
    )
    session_id = uuid4()
    now = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    submission = _submission(session_id, now)
    session_repository = PostgresSessionRepository(settings)
    run_repository = PostgresRunRepository(settings)
    event_repository = PostgresRuntimeEventRepository(settings)

    async def exercise() -> None:
        await session_repository.setup()
        await run_repository.setup()
        await event_repository.setup()
        await session_repository.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="RuntimeEvent 缺号",
                created_at=now,
                updated_at=now,
            )
        )
        await run_repository.create_or_get(submission)
        try:
            first_sequencer = RunSequencer.for_run(
                run_id=submission.run_id,
                response_message_id=submission.response_message_id,
                repository=event_repository,
                block_size=4,
            )
            first = await first_sequencer.emit(
                RuntimeEventDraft(
                    event_type="message.started",
                    source="executor",
                    visibility="public",
                    payload=MessageStartedPayload(
                        response_message_id=submission.response_message_id,
                        attempt=1,
                    ),
                    schema_version=1,
                    durability="durable",
                    created_at=now,
                )
            )

            del first_sequencer
            gc.collect()

            restarted_sequencer = RunSequencer.for_run(
                run_id=submission.run_id,
                response_message_id=submission.response_message_id,
                repository=event_repository,
                block_size=4,
            )
            restarted = await restarted_sequencer.emit(
                RuntimeEventDraft(
                    event_type="message.started",
                    source="executor",
                    visibility="public",
                    payload=MessageStartedPayload(
                        response_message_id=submission.response_message_id,
                        attempt=2,
                    ),
                    schema_version=1,
                    durability="durable",
                    created_at=now + timedelta(seconds=1),
                )
            )
            internal = await restarted_sequencer.emit(
                RuntimeEventDraft(
                    event_type="internal.execution.diagnostic",
                    source="executor",
                    visibility="internal",
                    payload=InternalDiagnosticPayload(
                        diagnostic_code="RESTARTED",
                    ),
                    schema_version=1,
                    durability="durable",
                    created_at=now + timedelta(seconds=2),
                )
            )
            transient = await restarted_sequencer.emit(
                RuntimeEventDraft(
                    event_type="message.delta",
                    source="executor",
                    visibility="public",
                    payload=MessageDeltaPayload(
                        response_message_id=submission.response_message_id,
                        attempt=2,
                        delta="瞬时增量",
                    ),
                    schema_version=1,
                    durability="transient",
                    created_at=now + timedelta(seconds=3),
                )
            )

            assert first.seq == 1
            assert restarted.seq == 5
            assert internal.seq == 6
            assert transient.seq == 7
            assert first.event_id.version == 4
            assert restarted.event_id.version == 4
            assert transient.event_id.version == 4

            with pytest.raises(RuntimeEventPersistenceError) as repeated_attempt:
                await restarted_sequencer.emit(
                    RuntimeEventDraft(
                        event_type="message.started",
                        source="executor",
                        visibility="public",
                        payload=MessageStartedPayload(
                            response_message_id=submission.response_message_id,
                            attempt=2,
                        ),
                        schema_version=1,
                        durability="durable",
                        created_at=now + timedelta(seconds=4),
                    )
                )
            assert repeated_attempt.value.code == (
                "RUNTIME_EVENT_MESSAGE_SEQUENCE_INVALID"
            )

            with pytest.raises(RuntimeEventSchemaError):
                await restarted_sequencer.emit(
                    RuntimeEventDraft(
                        event_type="message.started",
                        source="executor",
                        visibility="public",
                        payload=MessageStartedPayload(
                            response_message_id=uuid4(),
                            attempt=3,
                        ),
                        schema_version=1,
                        durability="durable",
                        created_at=now + timedelta(seconds=5),
                    )
                )

            public_events = await event_repository.list_public(
                run_id=submission.run_id,
                after_seq=0,
                limit=100,
            )
            assert [event.seq for event in public_events] == [1, 5]
            assert all(event.visibility == "public" for event in public_events)

            stored_run = await run_repository.get(submission.run_id)
            assert stored_run.seq_high_watermark == 8

            async with open_database_connection(settings) as connection:
                cursor = await connection.execute(
                    "SELECT seq FROM runtime_events WHERE run_id = %s ORDER BY seq",
                    (submission.run_id,),
                )
                rows = await cursor.fetchall()
            assert [row["seq"] for row in rows] == [1, 5, 6]
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


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
)
def test_state_event_transition_rejects_invalid_combinations_before_writes() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )
    from agent_runtime.runtime.event_schemas import (
        InterruptResumedPayload,
        RunStartedPayload,
    )
    from agent_runtime.runtime.repository import (
        PostgresRunRepository,
        RunStateConflictError,
    )
    from agent_runtime.runtime.sequencer import RunSequencer
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-event-transition-{uuid4()}",
        _env_file=None,
    )
    session_id = uuid4()
    now = datetime(2026, 9, 24, 10, 30, tzinfo=UTC)
    submission = _submission(session_id, now)
    session_repository = PostgresSessionRepository(settings)
    run_repository = PostgresRunRepository(settings)
    event_repository = PostgresRuntimeEventRepository(settings)

    def draft(event_type, payload, offset):
        return RuntimeEventDraft(
            event_type=event_type,
            source="executor",
            visibility="public",
            payload=payload,
            schema_version=1,
            durability="durable",
            created_at=now + timedelta(seconds=offset),
        )

    async def exercise() -> None:
        await session_repository.setup()
        await run_repository.setup()
        await event_repository.setup()
        await session_repository.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="状态事件三元组校验",
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
        try:
            interrupt_id = uuid4()
            with pytest.raises(RunStateConflictError):
                await sequencer.transition_run(
                    target_status="running",
                    event=draft(
                        "interrupt.resumed",
                        InterruptResumedPayload(interrupt_id=interrupt_id),
                        1,
                    ),
                    updated_at=now + timedelta(seconds=1),
                )

            after_invalid_resume = await run_repository.get(submission.run_id)
            assert after_invalid_resume.status == "queued"
            assert await event_repository.list_public(
                run_id=submission.run_id,
                after_seq=0,
                limit=100,
            ) == []

            started = await sequencer.transition_run(
                target_status="running",
                event=draft(
                    "run.started",
                    RunStartedPayload(status="running"),
                    2,
                ),
                updated_at=now + timedelta(seconds=2),
            )
            assert started.changed is True
            assert started.run.status == "running"
            assert started.event is not None
            assert started.event.event_type == "run.started"

            with pytest.raises(RunStateConflictError):
                await sequencer.transition_run(
                    target_status="running",
                    event=draft(
                        "run.started",
                        RunStartedPayload(status="running"),
                        3,
                    ),
                    updated_at=now + timedelta(seconds=3),
                )

            after_repeated_start = await run_repository.get(submission.run_id)
            assert after_repeated_start.status == "running"
            public_events = await event_repository.list_public(
                run_id=submission.run_id,
                after_seq=0,
                limit=100,
            )
            assert [event.event_type for event in public_events] == [
                "run.started"
            ]
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


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
)
def test_sequencer_serializes_concurrent_events_and_commits_one_terminal_atomically(
    monkeypatch,
) -> None:
    import agent_runtime.runtime.event_repository as event_repository_module
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.event_models import (
        RunEventCommit,
        RuntimeEventDraft,
    )
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
        RuntimeEventPersistenceError,
    )
    from agent_runtime.runtime.event_schemas import (
        MessageStartedPayload,
        RunCompletedPayload,
        RunFailedPayload,
        RunStartedPayload,
    )
    from agent_runtime.runtime.repository import (
        PostgresRunRepository,
        RunStateConflictError,
    )
    from agent_runtime.runtime.sequencer import RunSequencer
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-event-race-{uuid4()}",
        _env_file=None,
    )
    session_id = uuid4()
    now = datetime(2026, 9, 24, 11, 0, tzinfo=UTC)
    submission = _submission(session_id, now)
    session_repository = PostgresSessionRepository(settings)
    run_repository = PostgresRunRepository(settings)
    event_repository = PostgresRuntimeEventRepository(settings)

    def draft(event_type, payload, offset):
        return RuntimeEventDraft(
            event_type=event_type,
            source="executor",
            visibility="public",
            payload=payload,
            schema_version=1,
            durability="durable",
            created_at=now + timedelta(seconds=offset),
        )

    async def exercise() -> None:
        await session_repository.setup()
        await run_repository.setup()
        await event_repository.setup()
        await session_repository.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="RuntimeEvent 并发",
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
        try:
            started = await sequencer.transition_run(
                target_status="running",
                event=draft(
                    "run.started",
                    RunStartedPayload(status="running"),
                    1,
                ),
                updated_at=now + timedelta(seconds=1),
            )
            assert started.changed is True
            assert started.run.status == "running"
            assert started.event is not None

            message_events = await asyncio.gather(
                *[
                    sequencer.emit(
                        draft(
                            "message.started",
                            MessageStartedPayload(
                                response_message_id=(
                                    submission.response_message_id
                                ),
                                attempt=attempt,
                            ),
                            attempt + 1,
                        )
                    )
                    for attempt in range(1, 21)
                ]
            )
            message_sequences = [event.seq for event in message_events]
            assert len(message_sequences) == len(set(message_sequences))
            assert all(event.event_id.version == 4 for event in message_events)

            with monkeypatch.context() as patch_context:
                patch_context.setattr(
                    event_repository_module,
                    "_INSERT_RUNTIME_EVENT",
                    event_repository_module._INSERT_RUNTIME_EVENT.replace(
                        "INSERT INTO runtime_events",
                        "INSERT INTO missing_runtime_events",
                        1,
                    ),
                )
                with pytest.raises(RuntimeEventPersistenceError):
                    await sequencer.transition_run(
                        target_status="completed",
                        event=draft(
                            "run.completed",
                            RunCompletedPayload(status="completed"),
                            30,
                        ),
                        updated_at=now + timedelta(seconds=30),
                    )

            after_rollback = await run_repository.get(submission.run_id)
            assert after_rollback.status == "running"
            assert after_rollback.input_payload == submission.input_payload

            results = await asyncio.gather(
                sequencer.transition_run(
                    target_status="completed",
                    event=draft(
                        "run.completed",
                        RunCompletedPayload(status="completed"),
                        31,
                    ),
                    updated_at=now + timedelta(seconds=31),
                ),
                sequencer.transition_run(
                    target_status="failed",
                    event=draft(
                        "run.failed",
                        RunFailedPayload(
                            status="failed",
                            code="MODEL_FAILED",
                            message="模型执行失败",
                            retryable=False,
                        ),
                        32,
                    ),
                    updated_at=now + timedelta(seconds=32),
                    error_code="MODEL_FAILED",
                    error_message="仅写数据库，不进入业务日志",
                ),
                return_exceptions=True,
            )
            commits = [
                result
                for result in results
                if isinstance(result, RunEventCommit)
            ]
            conflicts = [
                result
                for result in results
                if isinstance(result, RunStateConflictError)
            ]
            assert len(commits) == 1
            assert len(conflicts) == 1

            terminal_run = await run_repository.get(submission.run_id)
            assert terminal_run.status in {"completed", "failed"}
            assert terminal_run.input_payload is None

            public_events = await event_repository.list_public(
                run_id=submission.run_id,
                after_seq=0,
                limit=100,
            )
            sequences = [event.seq for event in public_events]
            assert sequences == sorted(sequences)
            assert len(sequences) == len(set(sequences))
            terminal_events = [
                event
                for event in public_events
                if event.event_type in {"run.completed", "run.failed", "run.cancelled"}
            ]
            assert len(terminal_events) == 1
            assert terminal_events[0].event_type == f"run.{terminal_run.status}"
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
