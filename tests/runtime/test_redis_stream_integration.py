import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from redis.asyncio import Redis


def run_on_windows_compatible_loop(coroutine):
    """使用项目统一的 Windows 兼容事件循环执行异步集成测试。"""

    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coroutine)


def _public_event(
    *,
    run_id,
    response_message_id,
    seq: int,
    event_type: str,
    created_at: datetime,
):
    from agent_runtime.runtime.event_models import RuntimeEvent

    if event_type == "run.started":
        payload = {"status": "running"}
        durability = "durable"
    elif event_type == "message.started":
        payload = {
            "response_message_id": str(response_message_id),
            "attempt": 1,
        }
        durability = "durable"
    else:
        payload = {
            "response_message_id": str(response_message_id),
            "attempt": 1,
            "delta": f"增量-{seq}",
        }
        durability = "transient"
    return RuntimeEvent(
        event_id=uuid4(),
        run_id=run_id,
        seq=seq,
        event_type=event_type,
        source="executor",
        visibility="public",
        payload=payload,
        schema_version=1,
        durability=durability,
        created_at=created_at,
    )


@pytest.mark.redis
@pytest.mark.skipif(
    os.getenv("RUN_REDIS_TESTS") != "1",
    reason="设置 RUN_REDIS_TESTS=1 后运行 Redis 集成测试",
)
def test_redis_stream_recovers_same_publisher_without_backfilling_delta() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.runtime.redis_stream import RedisStreamPublisher

    settings = Settings()
    run_id = uuid4()
    response_message_id = uuid4()
    stream_key = f"runtime:events:{run_id}"
    now = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)

    async def exercise() -> None:
        client = Redis.from_url(
            settings.redis_connection_string,
            encoding="utf-8",
            decode_responses=True,
        )
        publisher = RedisStreamPublisher.from_url(
            settings.redis_connection_string,
            ttl_seconds=settings.redis_stream_ttl_seconds,
            socket_timeout_seconds=0.05,
        )
        await client.delete(stream_key)
        try:
            first = _public_event(
                run_id=run_id,
                response_message_id=response_message_id,
                seq=1,
                event_type="run.started",
                created_at=now,
            )
            missing_delta = _public_event(
                run_id=run_id,
                response_message_id=response_message_id,
                seq=2,
                event_type="message.delta",
                created_at=now + timedelta(seconds=1),
            )
            recovered_delta = _public_event(
                run_id=run_id,
                response_message_id=response_message_id,
                seq=3,
                event_type="message.delta",
                created_at=now + timedelta(seconds=2),
            )

            assert await publisher.publish(first) is True
            await client.execute_command("CLIENT", "PAUSE", 300, "WRITE")
            assert await publisher.publish(missing_delta) is False
            await asyncio.sleep(0.4)
            assert await publisher.publish(recovered_delta) is True

            entries = await client.xrange(stream_key)
            entry_ids = [entry_id for entry_id, _ in entries]
            # 超时只表示结果未确认；暂停解除后原 seq=2 事务仍可能执行，
            # 但应用不得重试它，且同一 publisher 必须能继续写入 seq=3。
            assert entry_ids in (["1-0", "3-0"], ["1-0", "2-0", "3-0"])
            decoded = [json.loads(fields["event"]) for _, fields in entries]
            assert [event["seq"] for event in decoded] == [
                int(entry_id.split("-", maxsplit=1)[0])
                for entry_id in entry_ids
            ]
            assert entry_ids[-1] == "3-0"
            assert all("source" not in event for event in decoded)
            ttl = await client.ttl(stream_key)
            assert 0 < ttl <= settings.redis_stream_ttl_seconds
        finally:
            await client.delete(stream_key)
            await publisher.aclose()
            await client.aclose()

    run_on_windows_compatible_loop(exercise())


@pytest.mark.postgres
@pytest.mark.redis
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1"
    or os.getenv("RUN_REDIS_TESTS") != "1",
    reason="同时设置 RUN_POSTGRES_TESTS=1 和 RUN_REDIS_TESTS=1 后运行组合测试",
)
def test_redis_outage_does_not_rollback_postgres_state_and_durable_event() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_repository import PostgresRuntimeEventRepository
    from agent_runtime.runtime.event_schemas import RunStartedPayload
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.models import RunSubmission
    from agent_runtime.runtime.redis_stream import RedisStreamPublisher
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.runtime.sequencer import RunSequencer
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-redis-outage-{uuid4()}",
        _env_file=None,
    )
    session_id = uuid4()
    run_id = uuid4()
    response_message_id = uuid4()
    request_id = uuid4()
    input_message_id = uuid4()
    now = datetime(2026, 9, 26, 11, 0, tzinfo=UTC)
    session_repository = PostgresSessionRepository(settings)
    run_repository = PostgresRunRepository(settings)
    event_repository = PostgresRuntimeEventRepository(settings)
    unavailable = RedisStreamPublisher.from_url(
        "redis://127.0.0.1:1/0",
        ttl_seconds=1800,
        socket_timeout_seconds=0.05,
    )

    async def exercise() -> None:
        await session_repository.setup()
        await run_repository.setup()
        await event_repository.setup()
        await session_repository.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="Redis 降级组合测试",
                created_at=now,
                updated_at=now,
            )
        )
        submission = RunSubmission(
            run_id=run_id,
            request_id=request_id,
            session_id=session_id,
            thread_id=str(session_id),
            parent_run_id=None,
            run_type="normal",
            input_message_id=input_message_id,
            response_message_id=response_message_id,
            start_checkpoint_id=None,
            input_payload={"message": {"content": "组合测试输入"}},
            request_fingerprint=build_request_fingerprint(
                run_type="normal",
                session_id=session_id,
                request_payload={"message": {"content": "组合测试输入"}},
            ),
            created_at=now,
        )
        await run_repository.create_or_get(submission)
        try:
            sequencer = RunSequencer.for_run(
                run_id=run_id,
                response_message_id=response_message_id,
                repository=event_repository,
                publisher=unavailable,
                block_size=8,
            )
            commit = await sequencer.transition_run(
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

            stored_run = await run_repository.get(run_id)
            stored_events = await event_repository.list_public(
                run_id=run_id,
                after_seq=0,
                limit=100,
            )
            assert commit.run.status == "running"
            assert stored_run.status == "running"
            assert [(event.seq, event.event_type) for event in stored_events] == [
                (1, "run.started")
            ]
        finally:
            await unavailable.aclose()
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

    run_on_windows_compatible_loop(exercise())
