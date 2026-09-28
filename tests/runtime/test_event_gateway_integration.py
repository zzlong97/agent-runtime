import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from redis.asyncio import Redis


def run_on_windows_compatible_loop(coroutine):
    """使用项目统一的 Windows 兼容事件循环执行组合集成测试。"""

    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coroutine)


@pytest.mark.postgres
@pytest.mark.redis
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1"
    or os.getenv("RUN_REDIS_TESTS") != "1",
    reason="同时设置 RUN_POSTGRES_TESTS=1 和 RUN_REDIS_TESTS=1 后运行组合测试",
)
def test_gateway_merges_reconnects_and_survives_expired_redis_delta() -> None:
    """真实后端应合并、续传，并在 Redis 丢失后保留持久终态。"""

    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.event_gateway import RuntimeEventGateway
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )
    from agent_runtime.runtime.event_schemas import (
        MessageDeltaPayload,
        MessageFinalizedPayload,
        MessageStartedPayload,
        RunCompletedPayload,
        RunStartedPayload,
    )
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.models import RunSubmission
    from agent_runtime.runtime.redis_stream import (
        RedisStreamPublisher,
        RedisStreamReader,
    )
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.runtime.sequencer import RunSequencer
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        redis_url=base_settings.redis_url,
        local_user_id=f"s25-gateway-{uuid4()}",
        _env_file=None,
    )
    session_id = uuid4()
    run_id = uuid4()
    response_message_id = uuid4()
    now = datetime(2026, 9, 27, 14, 0, tzinfo=UTC)
    stream_key = f"runtime:events:{run_id}"
    session_repository = PostgresSessionRepository(settings)
    run_repository = PostgresRunRepository(settings)
    event_repository = PostgresRuntimeEventRepository(settings)
    publisher = RedisStreamPublisher.from_url(
        settings.redis_connection_string,
        ttl_seconds=settings.redis_stream_ttl_seconds,
        socket_timeout_seconds=settings.redis_socket_timeout_seconds,
    )
    reader = RedisStreamReader.from_url(
        settings.redis_connection_string,
        socket_timeout_seconds=settings.redis_socket_timeout_seconds,
    )
    redis_client = Redis.from_url(
        settings.redis_connection_string,
        encoding="utf-8",
        decode_responses=True,
    )

    async def exercise() -> None:
        await session_repository.setup()
        await run_repository.setup()
        await event_repository.setup()
        await redis_client.delete(stream_key)
        await session_repository.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="SSE Gateway 组合测试",
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
            response_message_id=response_message_id,
            start_checkpoint_id=None,
            input_payload={"message": {"content": "Gateway 测试输入"}},
            request_fingerprint=build_request_fingerprint(
                run_type="normal",
                session_id=session_id,
                request_payload={"message": {"content": "Gateway 测试输入"}},
            ),
            created_at=now,
        )
        await run_repository.create_or_get(submission)
        try:
            sequencer = RunSequencer.for_run(
                run_id=run_id,
                response_message_id=response_message_id,
                repository=event_repository,
                publisher=publisher,
                block_size=8,
            )
            await sequencer.transition_run(
                target_status="running",
                event=RuntimeEventDraft(
                    event_type="run.started",
                    source="executor",
                    visibility="public",
                    payload=RunStartedPayload(status="running"),
                    schema_version=1,
                    durability="durable",
                    created_at=now,
                ),
                updated_at=now,
            )
            await sequencer.emit(
                RuntimeEventDraft(
                    event_type="message.started",
                    source="executor",
                    visibility="public",
                    payload=MessageStartedPayload(
                        response_message_id=response_message_id,
                        attempt=1,
                    ),
                    schema_version=1,
                    durability="durable",
                    created_at=now + timedelta(seconds=1),
                )
            )
            await sequencer.emit(
                RuntimeEventDraft(
                    event_type="message.delta",
                    source="executor",
                    visibility="public",
                    payload=MessageDeltaPayload(
                        response_message_id=response_message_id,
                        attempt=1,
                        delta="实时增量",
                    ),
                    schema_version=1,
                    durability="transient",
                    created_at=now + timedelta(seconds=2),
                )
            )
            await sequencer.emit(
                RuntimeEventDraft(
                    event_type="message.finalized",
                    source="executor",
                    visibility="public",
                    payload=MessageFinalizedPayload(
                        response_message_id=response_message_id,
                        runtime_status="completed",
                        capability_id="general_chat",
                    ),
                    schema_version=1,
                    durability="durable",
                    created_at=now + timedelta(seconds=3),
                )
            )
            await sequencer.transition_run(
                target_status="completed",
                event=RuntimeEventDraft(
                    event_type="run.completed",
                    source="executor",
                    visibility="public",
                    payload=RunCompletedPayload(status="completed"),
                    schema_version=1,
                    durability="durable",
                    created_at=now + timedelta(seconds=4),
                ),
                updated_at=now + timedelta(seconds=4),
            )

            gateway = RuntimeEventGateway(
                run_repository=run_repository,
                event_repository=event_repository,
                redis_reader=reader,
                poll_interval_seconds=0.001,
            )

            async def collect(after_seq: int):
                return [
                    item
                    async for item in gateway.stream(
                        run_id=run_id,
                        after_seq=after_seq,
                    )
                ]

            initial = await collect(0)
            assert [event.seq for event in initial] == [1, 2, 3, 4, 5]
            assert [event.event_type for event in initial] == [
                "run.started",
                "message.started",
                "message.delta",
                "message.finalized",
                "run.completed",
            ]

            reconnected = await collect(2)
            assert [event.seq for event in reconnected] == [3, 4, 5]

            await redis_client.delete(stream_key)
            expired = await collect(0)
            assert [event.seq for event in expired] == [1, 2, 4, 5]
            assert expired[-1].event_type == "run.completed"
        finally:
            await redis_client.delete(stream_key)
            async with open_database_connection(settings) as connection:
                await connection.execute(
                    "DELETE FROM runtime_events WHERE run_id = %s",
                    (run_id,),
                )
                await connection.execute(
                    "DELETE FROM runs WHERE run_id = %s",
                    (run_id,),
                )
                await connection.execute(
                    "DELETE FROM sessions WHERE session_id = %s",
                    (session_id,),
                )
                await connection.commit()
            await publisher.aclose()
            await reader.aclose()
            await redis_client.aclose()

    run_on_windows_compatible_loop(exercise())
