import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4


RUN_ID = UUID("00000000-0000-0000-0000-000000002550")
RESPONSE_MESSAGE_ID = UUID("00000000-0000-0000-0000-000000002551")
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _runtime_event(seq: int, event_type: str):
    from agent_runtime.runtime.event_models import RuntimeEvent

    if event_type == "message.started":
        payload = {
            "response_message_id": str(RESPONSE_MESSAGE_ID),
            "attempt": 1,
        }
        durability = "durable"
    elif event_type == "message.delta":
        payload = {
            "response_message_id": str(RESPONSE_MESSAGE_ID),
            "attempt": 1,
            "delta": f"增量-{seq}",
        }
        durability = "transient"
    elif event_type == "run.completed":
        payload = {"status": "completed"}
        durability = "durable"
    else:
        payload = {"status": "running"}
        durability = "durable"
    return RuntimeEvent(
        event_id=uuid4(),
        run_id=RUN_ID,
        seq=seq,
        event_type=event_type,
        source="executor",
        visibility="public",
        payload=payload,
        schema_version=1,
        durability=durability,
        created_at=NOW,
    )


def _run(*, status: str):
    from agent_runtime.runtime.models import Run

    return Run(
        run_id=RUN_ID,
        request_id=uuid4(),
        session_id=uuid4(),
        thread_id="gateway-test",
        parent_run_id=None,
        run_type="normal",
        input_message_id=uuid4(),
        response_message_id=RESPONSE_MESSAGE_ID,
        start_checkpoint_id=None,
        input_payload=None,
        request_fingerprint="a" * 64,
        status=status,
        recovery_attempts=0,
        seq_high_watermark=8,
        error_code=None,
        error_message=None,
        created_at=NOW,
        started_at=NOW,
        finished_at=NOW if status == "completed" else None,
        updated_at=NOW,
    )


class _FakeEventRepository:
    def __init__(self, events) -> None:
        self.events = list(events)
        self.calls: list[tuple[int, int]] = []

    async def list_public(self, *, run_id, after_seq, limit):
        assert run_id == RUN_ID
        self.calls.append((after_seq, limit))
        return [event for event in self.events if event.seq > after_seq][:limit]


class _TerminalCommitRaceEventRepository:
    """模拟终态事务在首次事件查询后提交。"""

    def __init__(self) -> None:
        self.calls = 0

    async def list_public(self, *, run_id, after_seq, limit):
        assert run_id == RUN_ID
        self.calls += 1
        if self.calls == 1:
            return []
        if after_seq < 7:
            return [_runtime_event(7, "run.completed")]
        return []


class _CrossStoreCommitRaceEventRepository:
    """模拟 PG 首次读取后，durable 提交而 Redis 高序号先被看见。"""

    def __init__(self) -> None:
        self.committed = False
        self.calls: list[int] = []

    async def list_public(self, *, run_id, after_seq, limit):
        assert run_id == RUN_ID
        self.calls.append(after_seq)
        if not self.committed:
            return []
        return [
            event
            for event in (
                _runtime_event(2, "message.started"),
                _runtime_event(4, "run.completed"),
            )
            if event.seq > after_seq
        ][:limit]


class _CrossStoreCommitRaceRedisReader:
    """读取 Redis 时表示更早的 durable 事务已经完成提交。"""

    def __init__(self, event_repository) -> None:
        from agent_runtime.runtime.event_schemas import project_public_event

        self._event_repository = event_repository
        self._delta = project_public_event(_runtime_event(3, "message.delta"))

    async def read_public(self, *, run_id, after_seq, limit):
        assert run_id == RUN_ID
        self._event_repository.committed = True
        if self._delta.seq > after_seq:
            return [self._delta]
        return []


class _ReverseCrossStoreRaceRedisReader:
    """首次读取为空，随后较低 transient event 变为可见。"""

    def __init__(self) -> None:
        from agent_runtime.runtime.event_schemas import project_public_event

        self.published = False
        self._delta = project_public_event(_runtime_event(3, "message.delta"))

    async def read_public(self, *, run_id, after_seq, limit):
        assert run_id == RUN_ID
        if self.published and self._delta.seq > after_seq:
            return [self._delta]
        return []


class _ReverseCrossStoreRaceEventRepository:
    """PG 查询前发布较低 delta，再返回较高 durable 终态。"""

    def __init__(self, redis_reader) -> None:
        self._redis_reader = redis_reader

    async def list_public(self, *, run_id, after_seq, limit):
        assert run_id == RUN_ID
        self._redis_reader.published = True
        if after_seq < 4:
            return [_runtime_event(4, "run.completed")]
        return []


class _FakeRedisReader:
    def __init__(self, events) -> None:
        self.events = list(events)
        self.calls: list[tuple[int, int]] = []

    async def read_public(self, *, run_id, after_seq, limit):
        assert run_id == RUN_ID
        self.calls.append((after_seq, limit))
        return [event for event in self.events if event.seq > after_seq][:limit]


class _FakeRunRepository:
    def __init__(self, status: str) -> None:
        self.run = _run(status=status)
        self.calls = 0

    async def get(self, run_id):
        assert run_id == RUN_ID
        self.calls += 1
        return self.run


def test_gateway_merges_sorts_deduplicates_and_allows_sequence_gaps() -> None:
    from agent_runtime.runtime.event_gateway import RuntimeEventGateway
    from agent_runtime.runtime.event_schemas import project_public_event

    durable = [
        _runtime_event(2, "message.started"),
        _runtime_event(7, "run.completed"),
    ]
    realtime = [
        project_public_event(durable[0]),
        project_public_event(_runtime_event(4, "message.delta")),
    ]
    gateway = RuntimeEventGateway(
        run_repository=_FakeRunRepository("completed"),
        event_repository=_FakeEventRepository(durable),
        redis_reader=_FakeRedisReader(realtime),
        batch_size=10,
        poll_interval_seconds=0.001,
        heartbeat_interval_seconds=1,
    )

    async def collect() -> list[int]:
        return [
            item.seq
            async for item in gateway.stream(run_id=RUN_ID, after_seq=0)
        ]

    assert asyncio.run(collect()) == [2, 4, 7]


def test_gateway_reconnect_emits_only_events_after_cursor() -> None:
    from agent_runtime.runtime.event_gateway import RuntimeEventGateway
    from agent_runtime.runtime.event_schemas import project_public_event

    gateway = RuntimeEventGateway(
        run_repository=_FakeRunRepository("completed"),
        event_repository=_FakeEventRepository(
            [
                _runtime_event(2, "message.started"),
                _runtime_event(7, "run.completed"),
            ]
        ),
        redis_reader=_FakeRedisReader(
            [project_public_event(_runtime_event(4, "message.delta"))]
        ),
        batch_size=10,
        poll_interval_seconds=0.001,
        heartbeat_interval_seconds=1,
    )

    async def collect() -> list[int]:
        return [
            item.seq
            async for item in gateway.stream(run_id=RUN_ID, after_seq=2)
        ]

    assert asyncio.run(collect()) == [4, 7]


def test_gateway_delivers_postgres_terminal_when_redis_stream_is_empty() -> None:
    from agent_runtime.runtime.event_gateway import RuntimeEventGateway

    gateway = RuntimeEventGateway(
        run_repository=_FakeRunRepository("completed"),
        event_repository=_FakeEventRepository(
            [_runtime_event(7, "run.completed")]
        ),
        redis_reader=_FakeRedisReader([]),
        batch_size=10,
        poll_interval_seconds=0.001,
        heartbeat_interval_seconds=1,
    )

    async def collect() -> list[str]:
        return [
            item.event_type
            async for item in gateway.stream(run_id=RUN_ID, after_seq=0)
        ]

    assert asyncio.run(collect()) == ["run.completed"]


def test_gateway_closes_after_delivering_interrupt_required() -> None:
    """中断投影送达后应结束当前 SSE，而不是等待 Run 进入终态。"""

    from agent_runtime.runtime.event_gateway import RuntimeEventGateway
    from agent_runtime.runtime.event_models import RuntimeEvent

    interrupt_event = RuntimeEvent(
        event_id=uuid4(),
        run_id=RUN_ID,
        seq=7,
        event_type="interrupt.required",
        source="executor",
        visibility="public",
        payload={"interrupt_id": str(uuid4())},
        schema_version=1,
        durability="durable",
        created_at=NOW,
    )
    gateway = RuntimeEventGateway(
        run_repository=_FakeRunRepository("interrupted"),
        event_repository=_FakeEventRepository([interrupt_event]),
        redis_reader=_FakeRedisReader([]),
        batch_size=10,
        poll_interval_seconds=0.001,
        heartbeat_interval_seconds=1,
    )

    async def collect() -> list[str]:
        return [
            item.event_type
            async for item in gateway.stream(run_id=RUN_ID, after_seq=0)
        ]

    assert asyncio.run(asyncio.wait_for(collect(), timeout=0.2)) == [
        "interrupt.required"
    ]


def test_gateway_rechecks_durable_events_before_closing_terminal_run() -> None:
    """终态事务在首次查询后提交时，关闭前仍须发送终态事件。"""

    from agent_runtime.runtime.event_gateway import RuntimeEventGateway

    event_repository = _TerminalCommitRaceEventRepository()
    gateway = RuntimeEventGateway(
        run_repository=_FakeRunRepository("completed"),
        event_repository=event_repository,
        redis_reader=_FakeRedisReader([]),
        batch_size=10,
        poll_interval_seconds=0.001,
        heartbeat_interval_seconds=1,
    )

    async def collect() -> list[str]:
        return [
            item.event_type
            async for item in gateway.stream(run_id=RUN_ID, after_seq=0)
        ]

    assert asyncio.run(collect()) == ["run.completed"]
    assert event_repository.calls >= 2


def test_gateway_does_not_advance_past_durable_committed_before_redis_delta() -> None:
    """较高 Redis seq 不得使本轮刚提交的较低 durable event 永久丢失。"""

    from agent_runtime.runtime.event_gateway import RuntimeEventGateway

    event_repository = _CrossStoreCommitRaceEventRepository()
    gateway = RuntimeEventGateway(
        run_repository=_FakeRunRepository("completed"),
        event_repository=event_repository,
        redis_reader=_CrossStoreCommitRaceRedisReader(event_repository),
        batch_size=10,
        poll_interval_seconds=0.001,
        heartbeat_interval_seconds=1,
    )

    async def collect() -> list[tuple[int, str]]:
        return [
            (item.seq, item.event_type)
            async for item in gateway.stream(run_id=RUN_ID, after_seq=0)
        ]

    assert asyncio.run(collect()) == [
        (2, "message.started"),
        (3, "message.delta"),
        (4, "run.completed"),
    ]


def test_gateway_rechecks_redis_before_advancing_to_higher_durable_event() -> None:
    """PG 返回较高 seq 前已发布的较低 transient event 不得被越过。"""

    from agent_runtime.runtime.event_gateway import RuntimeEventGateway

    redis_reader = _ReverseCrossStoreRaceRedisReader()
    gateway = RuntimeEventGateway(
        run_repository=_FakeRunRepository("completed"),
        event_repository=_ReverseCrossStoreRaceEventRepository(redis_reader),
        redis_reader=redis_reader,
        batch_size=10,
        poll_interval_seconds=0.001,
        heartbeat_interval_seconds=1,
    )

    async def collect() -> list[tuple[int, str]]:
        return [
            (item.seq, item.event_type)
            async for item in gateway.stream(run_id=RUN_ID, after_seq=2)
        ]

    assert asyncio.run(collect()) == [
        (3, "message.delta"),
        (4, "run.completed"),
    ]


def test_gateway_does_not_prefetch_next_batch_until_current_batch_is_consumed() -> None:
    from agent_runtime.runtime.event_gateway import RuntimeEventGateway

    event_repository = _FakeEventRepository(
        [
            _runtime_event(1, "run.started"),
            _runtime_event(2, "message.started"),
            _runtime_event(7, "run.completed"),
        ]
    )
    redis_reader = _FakeRedisReader([])
    gateway = RuntimeEventGateway(
        run_repository=_FakeRunRepository("completed"),
        event_repository=event_repository,
        redis_reader=redis_reader,
        batch_size=2,
        poll_interval_seconds=0.001,
        heartbeat_interval_seconds=1,
    )

    async def exercise() -> None:
        stream = gateway.stream(run_id=RUN_ID, after_seq=0)
        assert (await anext(stream)).seq == 1
        assert event_repository.calls == [(0, 2)]
        assert redis_reader.calls == [(0, 2), (0, 2)]
        assert (await anext(stream)).seq == 2
        assert event_repository.calls == [(0, 2)]
        assert redis_reader.calls == [(0, 2), (0, 2)]
        assert (await anext(stream)).seq == 7
        assert event_repository.calls == [(0, 2), (2, 2)]
        assert redis_reader.calls == [
            (0, 2),
            (0, 2),
            (2, 2),
            (2, 2),
        ]
        await stream.aclose()

    asyncio.run(exercise())


def test_gateway_emits_comment_heartbeat_without_allocating_sequence() -> None:
    from agent_runtime.runtime.event_gateway import (
        GatewayHeartbeat,
        RuntimeEventGateway,
    )

    gateway = RuntimeEventGateway(
        run_repository=_FakeRunRepository("running"),
        event_repository=_FakeEventRepository([]),
        redis_reader=_FakeRedisReader([]),
        batch_size=2,
        poll_interval_seconds=0.001,
        heartbeat_interval_seconds=0.005,
    )

    async def exercise() -> None:
        stream = gateway.stream(run_id=RUN_ID, after_seq=0)
        item = await asyncio.wait_for(anext(stream), timeout=0.2)
        assert isinstance(item, GatewayHeartbeat)
        await stream.aclose()

    asyncio.run(exercise())
