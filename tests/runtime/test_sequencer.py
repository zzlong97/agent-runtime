import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict, Field


class _InternalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diagnostic_code: str = Field(
        description="仅用于验证内部事件不会进入 Redis 公开传输的诊断代码。"
    )


class _SequencerRepository:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions

    async def reserve_sequence_block(self, *, run_id, block_size):
        from agent_runtime.runtime.event_models import SequenceBlock

        self.actions.append("reserve")
        return SequenceBlock(first=1, last=block_size)

    async def append_durable(self, event):
        self.actions.append("postgres")
        return event

    async def transition_run(self, **arguments):
        self.actions.append("postgres-transition")
        return SimpleNamespace(
            run=SimpleNamespace(status=arguments["target_status"]),
            event=arguments["event"],
            changed=True,
        )


class _SequencerPublisher:
    def __init__(self, actions: list[str], *, result: bool = True) -> None:
        self.actions = actions
        self.result = result
        self.events = []

    async def publish(self, event) -> bool:
        self.actions.append("redis")
        self.events.append(event)
        return self.result


def _run_started_draft(*, durability: str = "durable"):
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_schemas import RunStartedPayload

    return RuntimeEventDraft(
        event_type="run.started",
        source="executor",
        visibility="public",
        payload=RunStartedPayload(status="running"),
        schema_version=1,
        durability=durability,
        created_at=datetime(2026, 9, 26, 10, 0, tzinfo=UTC),
    )


def test_run_sequencer_registry_returns_one_live_instance_per_run() -> None:
    from agent_runtime.runtime.sequencer import RunSequencer

    run_id = uuid4()
    response_message_id = uuid4()
    repository = object()

    first = RunSequencer.for_run(
        run_id=run_id,
        response_message_id=response_message_id,
        repository=repository,
        block_size=8,
    )
    repeated = RunSequencer.for_run(
        run_id=run_id,
        response_message_id=response_message_id,
        repository=repository,
        block_size=8,
    )

    assert repeated is first
    with pytest.raises(TypeError):
        RunSequencer(
            run_id=run_id,
            response_message_id=response_message_id,
            repository=repository,
            block_size=8,
        )


def test_run_sequencer_registry_rejects_conflicting_live_identity() -> None:
    from agent_runtime.runtime.sequencer import RunSequencer

    run_id = uuid4()
    repository = object()
    first = RunSequencer.for_run(
        run_id=run_id,
        response_message_id=uuid4(),
        repository=repository,
        block_size=8,
    )
    assert first is not None

    with pytest.raises(ValueError):
        RunSequencer.for_run(
            run_id=run_id,
            response_message_id=uuid4(),
            repository=repository,
            block_size=8,
        )


def test_durable_event_commits_postgres_before_best_effort_redis() -> None:
    from agent_runtime.runtime.sequencer import RunSequencer

    actions: list[str] = []
    repository = _SequencerRepository(actions)
    publisher = _SequencerPublisher(actions, result=False)
    sequencer = RunSequencer.for_run(
        run_id=uuid4(),
        response_message_id=uuid4(),
        repository=repository,
        publisher=publisher,
        block_size=8,
    )

    event = asyncio.run(sequencer.emit(_run_started_draft()))

    assert actions == ["reserve", "postgres", "redis"]
    assert publisher.events == [event]


def test_transient_event_skips_postgres_and_publishes_to_redis() -> None:
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_schemas import (
        MessageDeltaPayload,
        MessageStartedPayload,
    )
    from agent_runtime.runtime.sequencer import RunSequencer

    actions: list[str] = []
    response_message_id = uuid4()
    repository = _SequencerRepository(actions)
    publisher = _SequencerPublisher(actions)
    sequencer = RunSequencer.for_run(
        run_id=uuid4(),
        response_message_id=response_message_id,
        repository=repository,
        publisher=publisher,
        block_size=8,
    )
    asyncio.run(
        sequencer.emit(
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
                created_at=datetime(2026, 9, 26, 9, 59, tzinfo=UTC),
            )
        )
    )
    actions.clear()
    publisher.events.clear()
    draft = RuntimeEventDraft(
        event_type="message.delta",
        source="executor",
        visibility="public",
        payload=MessageDeltaPayload(
            response_message_id=response_message_id,
            attempt=1,
            delta="公开增量",
        ),
        schema_version=1,
        durability="transient",
        created_at=datetime(2026, 9, 26, 10, 0, tzinfo=UTC),
    )

    event = asyncio.run(sequencer.emit(draft))

    assert actions == ["redis"]
    assert publisher.events == [event]


def test_state_event_commit_publishes_only_after_transaction_returns() -> None:
    from agent_runtime.runtime.sequencer import RunSequencer

    actions: list[str] = []
    repository = _SequencerRepository(actions)
    publisher = _SequencerPublisher(actions)
    sequencer = RunSequencer.for_run(
        run_id=uuid4(),
        response_message_id=uuid4(),
        repository=repository,
        publisher=publisher,
        block_size=8,
    )

    commit = asyncio.run(
        sequencer.transition_run(
            target_status="running",
            event=_run_started_draft(),
            updated_at=datetime(2026, 9, 26, 10, 0, tzinfo=UTC),
        )
    )

    assert actions == ["reserve", "postgres-transition", "redis"]
    assert publisher.events == [commit.event]


def test_internal_event_is_persisted_without_calling_public_publisher() -> None:
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.sequencer import RunSequencer

    actions: list[str] = []
    repository = _SequencerRepository(actions)
    publisher = _SequencerPublisher(actions)
    sequencer = RunSequencer.for_run(
        run_id=uuid4(),
        response_message_id=uuid4(),
        repository=repository,
        publisher=publisher,
        block_size=8,
    )

    event = asyncio.run(
        sequencer.emit(
            RuntimeEventDraft(
                event_type="internal.execution.diagnostic",
                source="executor",
                visibility="internal",
                payload=_InternalPayload(diagnostic_code="TEST"),
                schema_version=1,
                durability="durable",
                created_at=datetime(2026, 9, 26, 10, 0, tzinfo=UTC),
            )
        )
    )

    assert actions == ["reserve", "postgres"]
    assert event.visibility == "internal"
    assert publisher.events == []
