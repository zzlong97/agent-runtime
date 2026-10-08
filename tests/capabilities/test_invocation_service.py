import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest


class FakeInvocationEventRepository:
    def __init__(self) -> None:
        self.next_seq = 0
        self.events = []
        self.open_event = None

    async def reserve_sequence_block(self, *, run_id, block_size):
        from agent_runtime.runtime.event_models import SequenceBlock

        first = self.next_seq + 1
        self.next_seq += block_size
        return SequenceBlock(first=first, last=self.next_seq)

    async def begin_capability_invocation(self, event):
        from agent_runtime.runtime.event_models import (
            CapabilityInvocationEventCommit,
        )

        if self.open_event is not None:
            return CapabilityInvocationEventCommit(
                event=self.open_event,
                recovered=True,
            )
        self.open_event = event
        self.events.append(event)
        return CapabilityInvocationEventCommit(event=event, recovered=False)

    async def finish_capability_invocation(self, event):
        from agent_runtime.runtime.event_repository import (
            RuntimeEventPersistenceError,
        )

        if self.open_event is None:
            raise RuntimeEventPersistenceError(
                code="CAPABILITY_INVOCATION_NOT_OPEN",
                message="Capability Invocation 已结束或尚未开始",
                status_code=409,
            )
        if (
            event.payload["invocation_id"]
            != self.open_event.payload["invocation_id"]
        ):
            raise RuntimeEventPersistenceError(
                code="CAPABILITY_INVOCATION_MISMATCH",
                message="终态事件与当前 open Capability Invocation 不匹配",
                status_code=409,
            )
        self.events.append(event)
        self.open_event = None
        return event

    async def get_open_capability_invocation(self, *, run_id):
        return self.open_event


def test_invocation_service_recovers_open_id_and_allows_next_sequential_call() -> None:
    from agent_runtime.capabilities.invocation_service import (
        CapabilityInvocationService,
    )
    from agent_runtime.runtime.sequencer import RunSequencer

    run_id = uuid4()
    response_message_id = uuid4()
    first_id = UUID("00000000-0000-4000-8000-000000000201")
    unused_id = UUID("00000000-0000-4000-8000-000000000202")
    next_id = UUID("00000000-0000-4000-8000-000000000203")
    ids = iter((first_id, unused_id, next_id))
    repository = FakeInvocationEventRepository()
    sequencer = RunSequencer.for_run(
        run_id=run_id,
        response_message_id=response_message_id,
        repository=repository,
        block_size=4,
    )
    service = CapabilityInvocationService(
        sequencer=sequencer,
        invocation_id_factory=ids.__next__,
    )
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)

    async def exercise() -> None:
        first = await service.start(
            capability_id="general_chat",
            task_action="new",
            state_scope="session",
            created_at=now,
        )
        recovered = await service.start(
            capability_id="en_to_zh",
            task_action="continue",
            state_scope="invocation",
            created_at=now + timedelta(seconds=1),
        )
        assert first.invocation_id == first_id
        assert first.recovered is False
        assert recovered.invocation_id == first_id
        assert recovered.capability_id == "general_chat"
        assert recovered.task_action == "new"
        assert recovered.state_scope == "session"
        assert recovered.recovered is True

        completed = await service.complete(
            invocation_id=first_id,
            capability_id="general_chat",
            task_id=uuid4(),
            requested_task_action="new",
            effective_task_action="new",
            state_scope="session",
            state_schema_version="v1",
            outcome="completed",
            created_at=now + timedelta(seconds=2),
        )
        assert completed.event_type == "internal.capability.invocation.completed"

        second = await service.start(
            capability_id="en_to_zh",
            task_action="continue",
            state_scope="invocation",
            created_at=now + timedelta(seconds=3),
        )
        assert second.invocation_id == next_id
        assert second.recovered is False

    asyncio.run(exercise())


def test_invocation_service_rejects_terminal_for_different_open_id() -> None:
    from agent_runtime.capabilities.invocation_service import (
        CapabilityInvocationService,
    )
    from agent_runtime.runtime.event_repository import (
        RuntimeEventPersistenceError,
    )
    from agent_runtime.runtime.sequencer import RunSequencer

    repository = FakeInvocationEventRepository()
    sequencer = RunSequencer.for_run(
        run_id=uuid4(),
        response_message_id=uuid4(),
        repository=repository,
        block_size=4,
    )
    service = CapabilityInvocationService(sequencer=sequencer)

    async def exercise() -> None:
        started = await service.start(
            capability_id="general_chat",
            task_action="new",
            state_scope="session",
            created_at=datetime.now(UTC),
        )
        with pytest.raises(RuntimeEventPersistenceError) as caught:
            await service.fail(
                invocation_id=uuid4(),
                capability_id=started.capability_id,
                task_id=None,
                requested_task_action="new",
                effective_task_action=None,
                state_scope="session",
                state_schema_version=None,
                error_code="CAPABILITY_EXECUTION_FAILED",
                created_at=datetime.now(UTC),
            )
        assert caught.value.code == "CAPABILITY_INVOCATION_MISMATCH"

    asyncio.run(exercise())


def test_generic_event_append_cannot_bypass_invocation_transaction() -> None:
    from agent_runtime.runtime.event_models import RuntimeEvent
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
        RuntimeEventPersistenceError,
    )

    event = RuntimeEvent(
        event_id=uuid4(),
        run_id=uuid4(),
        seq=1,
        event_type="internal.capability.invocation.started",
        source="runtime.capability",
        visibility="internal",
        payload={
            "invocation_id": str(uuid4()),
            "capability_id": "general_chat",
            "task_id": None,
            "requested_task_action": "new",
            "effective_task_action": None,
            "state_scope": "session",
            "state_schema_version": None,
        },
        schema_version=1,
        durability="durable",
        created_at=datetime.now(UTC),
    )

    with pytest.raises(RuntimeEventPersistenceError) as caught:
        PostgresRuntimeEventRepository._validate_durable_event(
            event,
            allow_stateful=False,
        )
    assert caught.value.code == "RUNTIME_EVENT_REQUIRES_CAPABILITY_TRANSACTION"
