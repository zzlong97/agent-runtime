"""S2.5-06 Cancel 状态事务与首终态竞争的真实 PostgreSQL 验证。"""

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
def test_cancel_request_is_idempotent_and_wins_before_completion() -> None:
    """取消请求先写入时，完成事件不得覆盖 cancel_requested。"""

    from agent_runtime.core.config import Settings
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )
    from agent_runtime.runtime.event_schemas import (
        RunCancelRequestedPayload,
        RunCancelledPayload,
        RunCompletedPayload,
        RunStartedPayload,
    )
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.models import RunSubmission
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
        local_user_id=f"s25-cancel-state-{uuid4()}",
        _env_file=None,
    )
    session_id = uuid4()
    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
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
        input_payload={"message": {"content": "取消事务验收"}},
        request_fingerprint=build_request_fingerprint(
            run_type="normal",
            session_id=session_id,
            request_payload={"message": {"content": "取消事务验收"}},
        ),
        created_at=now,
    )

    def draft(event_type, payload, created_at):
        return RuntimeEventDraft(
            event_type=event_type,
            source="runtime.executor",
            visibility="public",
            payload=payload,
            schema_version=1,
            durability="durable",
            created_at=created_at,
        )

    def internal_draft(event_type, payload, created_at):
        return RuntimeEventDraft(
            event_type=event_type,
            source="runtime.api",
            visibility="internal",
            payload=payload,
            schema_version=1,
            durability="durable",
            created_at=created_at,
        )

    async def exercise() -> None:
        sessions = PostgresSessionRepository(settings)
        runs = PostgresRunRepository(settings)
        events = PostgresRuntimeEventRepository(settings)
        await sessions.setup()
        await runs.setup()
        await events.setup()
        await sessions.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="取消事务验收",
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
        try:
            await sequencer.transition_run(
                target_status="running",
                event=draft(
                    "run.started",
                    RunStartedPayload(status="running"),
                    now,
                ),
                updated_at=now,
            )
            requested = await sequencer.transition_run(
                target_status="cancel_requested",
                event=internal_draft(
                    "internal.run.cancel_requested",
                    RunCancelRequestedPayload(status="cancel_requested"),
                    now + timedelta(seconds=1),
                ),
                updated_at=now + timedelta(seconds=1),
            )
            repeated = await runs.get(submission.run_id)
            assert requested.run.status == repeated.status == "cancel_requested"

            async with open_database_connection(settings) as connection:
                cursor = await connection.execute(
                    "SELECT event_type, visibility FROM runtime_events "
                    "WHERE run_id = %s ORDER BY seq",
                    (submission.run_id,),
                )
                stored_events = await cursor.fetchall()
            assert [row["event_type"] for row in stored_events] == [
                "run.started",
                "internal.run.cancel_requested",
            ]
            assert stored_events[-1]["visibility"] == "internal"

            with pytest.raises(RunStateConflictError):
                await sequencer.transition_run(
                    target_status="completed",
                    event=draft(
                        "run.completed",
                        RunCompletedPayload(status="completed"),
                        now + timedelta(seconds=3),
                    ),
                    updated_at=now + timedelta(seconds=3),
                )

            cancelled = await sequencer.transition_run(
                target_status="cancelled",
                event=draft(
                    "run.cancelled",
                    RunCancelledPayload(status="cancelled"),
                    now + timedelta(seconds=4),
                ),
                updated_at=now + timedelta(seconds=4),
            )
            assert cancelled.run.status == "cancelled"
            assert cancelled.run.input_payload is None
            assert (await runs.get(submission.run_id)).status == "cancelled"
            public_events = await events.list_public(
                run_id=submission.run_id,
                after_seq=0,
                limit=100,
            )
            assert [event.event_type for event in public_events] == [
                "run.started",
                "run.cancelled",
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

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(exercise())
