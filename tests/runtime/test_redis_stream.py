import asyncio
import json
import logging
from datetime import UTC, datetime
from uuid import uuid4


class _FakePipeline:
    def __init__(self, outcomes: list[Exception | None]) -> None:
        self._outcomes = outcomes
        self.commands: list[tuple[object, ...]] = []

    def xadd(
        self,
        name: str,
        fields: dict[str, str],
        *,
        id: str,
    ) -> "_FakePipeline":
        self.commands.append(("xadd", name, fields, id))
        return self

    def expire(self, name: str, seconds: int) -> "_FakePipeline":
        self.commands.append(("expire", name, seconds))
        return self

    async def execute(self) -> list[object]:
        outcome = self._outcomes.pop(0) if self._outcomes else None
        if outcome is not None:
            raise outcome
        return ["1-0", True]


class _FakeRedis:
    def __init__(self, *outcomes: Exception | None) -> None:
        self.outcomes = list(outcomes)
        self.transactions: list[bool] = []
        self.pipelines: list[_FakePipeline] = []
        self.closed = False

    def pipeline(self, *, transaction: bool) -> _FakePipeline:
        self.transactions.append(transaction)
        pipeline = _FakePipeline(self.outcomes)
        self.pipelines.append(pipeline)
        return pipeline

    async def aclose(self) -> None:
        self.closed = True


class _FakeRedisReaderClient:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result or []
        self.error = error
        self.calls: list[tuple[dict[str, str], int]] = []
        self.closed = False

    async def xread(self, streams, *, count):
        self.calls.append((streams, count))
        if self.error is not None:
            raise self.error
        return self.result

    async def aclose(self) -> None:
        self.closed = True


def _public_event(
    *,
    seq: int = 1,
    event_type: str = "run.started",
    run_id=None,
):
    from agent_runtime.runtime.event_models import RuntimeEvent

    payload = {"status": "running"}
    durability = "durable"
    if event_type == "message.delta":
        payload = {
            "response_message_id": str(uuid4()),
            "attempt": 1,
            "delta": "公开增量",
        }
        durability = "transient"
    return RuntimeEvent(
        event_id=uuid4(),
        run_id=run_id or uuid4(),
        seq=seq,
        event_type=event_type,
        source="executor",
        visibility="public",
        payload=payload,
        schema_version=1,
        durability=durability,
        created_at=datetime(2026, 9, 26, 10, 0, tzinfo=UTC),
    )


def test_redis_stream_uses_fixed_key_sequence_id_and_refreshes_ttl() -> None:
    from agent_runtime.runtime.redis_stream import RedisStreamPublisher

    client = _FakeRedis(None)
    event = _public_event(seq=7)
    publisher = RedisStreamPublisher(client=client, ttl_seconds=1800)

    published = asyncio.run(publisher.publish(event))

    assert published is True
    assert client.transactions == [True]
    assert len(client.pipelines) == 1
    stream_key = f"runtime:events:{event.run_id}"
    xadd, expire = client.pipelines[0].commands
    assert xadd[0] == "xadd"
    assert xadd[1] == stream_key
    assert xadd[3] == "7-0"
    assert expire == ("expire", stream_key, 1800)

    public_body = json.loads(xadd[2]["event"])
    assert public_body["event_id"] == str(event.event_id)
    assert public_body["run_id"] == str(event.run_id)
    assert public_body["seq"] == 7
    assert public_body["event_type"] == "run.started"
    assert public_body["payload"] == {"status": "running"}
    assert "source" not in public_body
    assert "visibility" not in public_body
    assert "durability" not in public_body


def test_redis_failure_is_degraded_without_retry_or_delta_backfill() -> None:
    from agent_runtime.runtime.redis_stream import RedisStreamPublisher

    client = _FakeRedis(ConnectionError("redis://user:secret@localhost"), None)
    publisher = RedisStreamPublisher(client=client, ttl_seconds=1800)
    run_id = uuid4()
    failed_delta = _public_event(
        seq=2,
        event_type="message.delta",
        run_id=run_id,
    )
    recovered_delta = _public_event(
        seq=3,
        event_type="message.delta",
        run_id=run_id,
    )

    first_result = asyncio.run(publisher.publish(failed_delta))
    second_result = asyncio.run(publisher.publish(recovered_delta))

    assert first_result is False
    assert second_result is True
    assert len(client.pipelines) == 2
    assert client.pipelines[0].commands[0][3] == "2-0"
    assert client.pipelines[1].commands[0][3] == "3-0"
    assert all(
        command[3] != "2-0"
        for command in client.pipelines[1].commands
        if command[0] == "xadd"
    )


def test_redis_publisher_rejects_internal_event_before_client_write() -> None:
    from agent_runtime.runtime.event_models import RuntimeEvent
    from agent_runtime.runtime.redis_stream import RedisStreamPublisher

    client = _FakeRedis(None)
    event = RuntimeEvent(
        event_id=uuid4(),
        run_id=uuid4(),
        seq=1,
        event_type="internal.execution.diagnostic",
        source="executor",
        visibility="internal",
        payload={"diagnostic_code": "TEST"},
        schema_version=1,
        durability="durable",
        created_at=datetime(2026, 9, 26, 10, 0, tzinfo=UTC),
    )
    publisher = RedisStreamPublisher(client=client, ttl_seconds=1800)

    published = asyncio.run(publisher.publish(event))

    assert published is False
    assert client.pipelines == []


def test_redis_failure_log_does_not_leak_error_or_log_each_delta(caplog) -> None:
    from agent_runtime.runtime.redis_stream import RedisStreamPublisher

    client = _FakeRedis(
        ConnectionError("redis://user:secret@localhost"),
        ConnectionError("redis://user:secret@localhost"),
    )
    publisher = RedisStreamPublisher(client=client, ttl_seconds=1800)

    with caplog.at_level(logging.WARNING, logger="agent_runtime.runtime.redis_stream"):
        durable_result = asyncio.run(publisher.publish(_public_event(seq=1)))
        delta_result = asyncio.run(
            publisher.publish(_public_event(seq=2, event_type="message.delta"))
        )

    assert durable_result is False
    assert delta_result is False
    assert "Redis实时事件发布降级" in caplog.text
    assert "ConnectionError" in caplog.text
    assert "user:secret" not in caplog.text
    assert caplog.text.count("Redis实时事件发布降级") == 1


def test_redis_publisher_closes_injected_client() -> None:
    from agent_runtime.runtime.redis_stream import RedisStreamPublisher

    client = _FakeRedis()
    publisher = RedisStreamPublisher(client=client, ttl_seconds=1800)

    asyncio.run(publisher.aclose())

    assert client.closed is True


def test_redis_reader_reads_valid_public_events_after_cursor() -> None:
    from agent_runtime.runtime.event_schemas import project_public_event
    from agent_runtime.runtime.redis_stream import RedisStreamReader

    event = _public_event(seq=3)
    body = project_public_event(event).model_dump_json()
    stream_key = f"runtime:events:{event.run_id}"
    client = _FakeRedisReaderClient(
        [(stream_key, [("3-0", {"event": body})])]
    )
    reader = RedisStreamReader(client=client)

    events = asyncio.run(
        reader.read_public(run_id=event.run_id, after_seq=2, limit=10)
    )

    assert [item.seq for item in events] == [3]
    assert client.calls == [({stream_key: "2-0"}, 10)]


def test_redis_reader_rejects_stream_id_with_nonzero_suffix(caplog) -> None:
    from agent_runtime.runtime.event_schemas import project_public_event
    from agent_runtime.runtime.redis_stream import RedisStreamReader

    event = _public_event(seq=3)
    body = project_public_event(event).model_dump_json()
    stream_key = f"runtime:events:{event.run_id}"
    client = _FakeRedisReaderClient(
        [(stream_key, [("3-1", {"event": body})])]
    )
    reader = RedisStreamReader(client=client)

    with caplog.at_level(
        logging.WARNING,
        logger="agent_runtime.runtime.redis_stream",
    ):
        events = asyncio.run(
            reader.read_public(run_id=event.run_id, after_seq=2, limit=10)
        )

    assert events == []
    assert "Redis实时事件读取拒绝" in caplog.text


def test_redis_reader_failure_degrades_without_leaking_connection(caplog) -> None:
    from agent_runtime.runtime.redis_stream import RedisStreamReader

    run_id = uuid4()
    client = _FakeRedisReaderClient(
        error=ConnectionError("redis://user:secret@localhost")
    )
    reader = RedisStreamReader(client=client)

    with caplog.at_level(
        logging.WARNING,
        logger="agent_runtime.runtime.redis_stream",
    ):
        events = asyncio.run(
            reader.read_public(run_id=run_id, after_seq=0, limit=10)
        )

    assert events == []
    assert "REDIS_EVENT_READ_FAILED" in caplog.text
    assert "user:secret" not in caplog.text
