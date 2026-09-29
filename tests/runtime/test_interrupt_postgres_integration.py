"""S2.5-07 Interrupt / Resume 事务与幂等真实 PostgreSQL 验证。"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
)
def test_interrupt_resume_is_atomic_idempotent_and_cancel_closes_pending() -> None:
    """中断投影、恢复、冲突拒绝和取消必须保持单事务一致。"""

    from psycopg.errors import UniqueViolation
    from psycopg.types.json import Jsonb

    from agent_runtime.core.config import Settings
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )
    from agent_runtime.runtime.event_schemas import (
        InterruptRequiredPayload,
        InterruptResumedPayload,
        RunCancelledPayload,
        RunStartedPayload,
    )
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.interrupts import (
        InterruptRequestConflictError,
        InterruptStateConflictError,
        PostgresRunInterruptRepository,
        build_resume_request_fingerprint,
    )
    from agent_runtime.runtime.models import RunSubmission
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.runtime.sequencer import RunSequencer
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-interrupt-{uuid4()}",
        _env_file=None,
    )
    session_id = uuid4()
    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
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
        input_payload={"message": {"content": "中断事务验收"}},
        request_fingerprint=build_request_fingerprint(
            run_type="normal",
            session_id=session_id,
            request_payload={"message": {"content": "中断事务验收"}},
        ),
        created_at=now,
    )

    def draft(event_type, payload, offset):
        return RuntimeEventDraft(
            event_type=event_type,
            source="runtime.executor",
            visibility="public",
            payload=payload,
            schema_version=1,
            durability="durable",
            created_at=now + timedelta(seconds=offset),
        )

    async def exercise() -> None:
        sessions = PostgresSessionRepository(settings)
        runs = PostgresRunRepository(settings)
        events = PostgresRuntimeEventRepository(settings)
        interrupts = PostgresRunInterruptRepository(settings)
        await sessions.setup()
        await runs.setup()
        await events.setup()
        await sessions.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="Interrupt / Resume 验收",
                created_at=now,
                updated_at=now,
            )
        )
        await runs.create_or_get(submission)
        sequencer = RunSequencer.for_run(
            run_id=submission.run_id,
            response_message_id=submission.response_message_id,
            repository=events,
        )
        first_interrupt_id = uuid4()
        second_interrupt_id = uuid4()
        request_id = uuid4()
        resume_payload = {"approved": True}
        fingerprint = build_resume_request_fingerprint(
            run_id=submission.run_id,
            interrupt_id=first_interrupt_id,
            resume_payload=resume_payload,
        )
        try:
            await sequencer.transition_run(
                target_status="running",
                event=draft(
                    "run.started",
                    RunStartedPayload(status="running"),
                    0,
                ),
                updated_at=now,
            )
            required = await sequencer.require_interrupt(
                interrupt_id=first_interrupt_id,
                interrupt_payload={"prompt": "是否允许继续执行？"},
                event=draft(
                    "interrupt.required",
                    InterruptRequiredPayload(
                        interrupt_id=first_interrupt_id
                    ),
                    1,
                ),
                updated_at=now + timedelta(seconds=1),
            )
            assert required.run.status == "interrupted"
            assert required.interrupt.status == "pending"
            assert (await interrupts.get_pending(run_id=submission.run_id)) == (
                required.interrupt
            )

            async with open_database_connection(settings) as connection:
                with pytest.raises(UniqueViolation):
                    await connection.execute(
                        "INSERT INTO run_interrupts "
                        "(run_id, interrupt_id, status, interrupt_payload, created_at) "
                        "VALUES (%s, %s, 'pending', %s, %s)",
                        (
                            submission.run_id,
                            uuid4(),
                            Jsonb({"prompt": "不允许并行"}),
                            now + timedelta(seconds=2),
                        ),
                    )
                await connection.rollback()

            resumed = await sequencer.resume_interrupt(
                interrupt_id=first_interrupt_id,
                request_id=request_id,
                request_fingerprint=fingerprint,
                resume_payload=resume_payload,
                event=draft(
                    "interrupt.resumed",
                    InterruptResumedPayload(
                        interrupt_id=first_interrupt_id
                    ),
                    3,
                ),
                updated_at=now + timedelta(seconds=3),
            )
            assert resumed.changed is True
            assert resumed.run.run_id == submission.run_id
            assert resumed.run.status == "running"
            assert resumed.interrupt.status == "resumed"

            repeated = await sequencer.resume_interrupt(
                interrupt_id=first_interrupt_id,
                request_id=request_id,
                request_fingerprint=fingerprint,
                resume_payload=resume_payload,
                event=draft(
                    "interrupt.resumed",
                    InterruptResumedPayload(
                        interrupt_id=first_interrupt_id
                    ),
                    4,
                ),
                updated_at=now + timedelta(seconds=4),
            )
            assert repeated.changed is False
            assert repeated.event is None
            assert repeated.run.run_id == resumed.run.run_id

            conflicting_payload = {"approved": False}
            with pytest.raises(InterruptRequestConflictError):
                await sequencer.resume_interrupt(
                    interrupt_id=first_interrupt_id,
                    request_id=request_id,
                    request_fingerprint=build_resume_request_fingerprint(
                        run_id=submission.run_id,
                        interrupt_id=first_interrupt_id,
                        resume_payload=conflicting_payload,
                    ),
                    resume_payload=conflicting_payload,
                    event=draft(
                        "interrupt.resumed",
                        InterruptResumedPayload(
                            interrupt_id=first_interrupt_id
                        ),
                        5,
                    ),
                    updated_at=now + timedelta(seconds=5),
                )

            with pytest.raises(InterruptStateConflictError):
                await sequencer.resume_interrupt(
                    interrupt_id=first_interrupt_id,
                    request_id=uuid4(),
                    request_fingerprint=fingerprint,
                    resume_payload=resume_payload,
                    event=draft(
                        "interrupt.resumed",
                        InterruptResumedPayload(
                            interrupt_id=first_interrupt_id
                        ),
                        6,
                    ),
                    updated_at=now + timedelta(seconds=6),
                )

            await sequencer.require_interrupt(
                interrupt_id=second_interrupt_id,
                interrupt_payload={"prompt": "是否执行下一步？"},
                event=draft(
                    "interrupt.required",
                    InterruptRequiredPayload(
                        interrupt_id=second_interrupt_id
                    ),
                    7,
                ),
                updated_at=now + timedelta(seconds=7),
            )
            cancelled = await sequencer.transition_run(
                target_status="cancelled",
                event=draft(
                    "run.cancelled",
                    RunCancelledPayload(status="cancelled"),
                    8,
                ),
                updated_at=now + timedelta(seconds=8),
            )
            assert cancelled.run.status == "cancelled"
            assert await interrupts.get_pending(run_id=submission.run_id) is None

            async with open_database_connection(settings) as connection:
                cursor = await connection.execute(
                    "SELECT status FROM run_interrupts "
                    "WHERE run_id = %s AND interrupt_id = %s",
                    (submission.run_id, second_interrupt_id),
                )
                cancelled_interrupt = await cursor.fetchone()
                cursor = await connection.execute(
                    "SELECT COUNT(*) AS total FROM runs WHERE run_id = %s",
                    (submission.run_id,),
                )
                run_count = await cursor.fetchone()
            assert cancelled_interrupt["status"] == "cancelled"
            assert run_count["total"] == 1
            public_events = await events.list_public(
                run_id=submission.run_id,
                after_seq=0,
                limit=100,
            )
            assert [event.event_type for event in public_events] == [
                "run.started",
                "interrupt.required",
                "interrupt.resumed",
                "interrupt.required",
                "run.cancelled",
            ]
        finally:
            async with open_database_connection(settings) as connection:
                await connection.execute(
                    "DELETE FROM run_interrupts WHERE run_id = %s",
                    (submission.run_id,),
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

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(exercise())
