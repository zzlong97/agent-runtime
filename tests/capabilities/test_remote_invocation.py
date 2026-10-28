"""远程 Adapter 的稳定传输、绑定及只读恢复契约。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from agent_runtime.capabilities.manifest import CapabilityManifest
from agent_runtime.capabilities.remote_a2a.capability import RemoteA2ACapability
from agent_runtime.runtime.agent_contract import AgentCancellation, AgentEventOutlet, RunContext, TaskInput
from tests.capabilities.test_manifest import _valid_manifest
from tests.fake_a2a import FakeA2A, remote_transport


class MemoryRemoteEvents:
    """保留跨 Adapter 重建的已提交事实，不模拟远程业务结果。"""

    def __init__(self):
        self.events = {}
        self.identity_provider = None
        self.original_identity = None

    async def get_open_capability_invocation(self):
        if self.original_identity is None:
            self.original_identity = self.identity_provider()
        return SimpleNamespace(payload={"remote_agent_identity_hash": self.original_identity})

    async def get_remote_input_interrupts(self, *, invocation_id):
        return []

    async def get_remote_agent_event(self, *, invocation_id, event_type):
        return self.events.get((invocation_id, event_type))

    async def record_remote_agent_event(self, draft):
        key = (draft.payload.invocation_id, draft.event_type)
        event = SimpleNamespace(payload=draft.payload.model_dump(mode="json"))
        previous = self.events.setdefault(key, event)
        assert previous.payload == event.payload
        return previous


class MemoryOperations:
    def __init__(self):
        self.rows = {}

    @asynccontextmanager
    async def operation(self, key):
        created = key not in self.rows
        self.rows.setdefault(key, "pending")
        async def mark_failed():
            self.rows[key] = "failed"
        yield SimpleNamespace(should_execute=self.rows[key] == "pending", mark_failed=mark_failed, created=created)
        if self.rows[key] == "pending":
            self.rows[key] = "succeeded"


async def never_cancel():
    return False


def remote_fixture(fake=None):
    raw = _valid_manifest()
    raw["transport"] = remote_transport()
    raw["side_effect_policy"] = "idempotent"
    fake = fake or FakeA2A()
    adapter = RemoteA2ACapability(
        manifest=CapabilityManifest.model_validate(raw),
        development_mode=True,
        http_transport=fake.transport,
    )
    invocation_id = uuid4()
    operations = MemoryOperations()
    gateway = SimpleNamespace(run_id=uuid4(), invocation_id=invocation_id)
    gateway.operation = operations.operation
    output = []
    context = RunContext(
        run_id=gateway.run_id, invocation_id=invocation_id,
        request_id=uuid4(), input_message_id=uuid4(), response_message_id=uuid4(),
        cancellation=AgentCancellation(never_cancel), events=AgentEventOutlet(output.append),
        operation_gateway=gateway,
    )
    task_input = TaskInput(messages=(SystemMessage(content="秘密系统消息"), HumanMessage(content="仅当前请求")))
    events = MemoryRemoteEvents()
    events.identity_provider = lambda: adapter.client.snapshot.fingerprint
    return fake, adapter, context, task_input, events, operations, output


def test_completed_records_binding_before_send_success_and_replays_only_get():
    async def exercise():
        fake, adapter, context, task_input, events, operations, output = remote_fixture()
        await adapter.initialize()
        try:
            result = await adapter.invoke_bound(context, None, task_input, sequencer=events)
            assert result.content == "第一段第二段"
            assert len(output) == 1
            assert len(events.events) == 2
            assert len(operations.rows) == 1
            assert set(operations.rows.values()) == {"succeeded"}
            await adapter.invoke_bound(context, None, task_input, sequencer=events)
            assert sum(r.url.path.endswith("/message:send") for r in fake.requests) == 1
            assert len(fake.tasks) == 1
            assert "秘密系统消息" not in fake.requests[1].content.decode()
        finally:
            await adapter.close()
    asyncio.run(exercise())


def test_bound_send_left_pending_is_reconciled_without_resending():
    async def exercise():
        fake, adapter, context, task_input, events, operations, output = remote_fixture()
        await adapter.initialize()
        try:
            await adapter.invoke_bound(context, None, task_input, sequencer=events)
            key = next(iter(operations.rows))
            operations.rows[key] = "pending"
            await adapter.invoke_bound(context, None, task_input, sequencer=events)
            assert operations.rows[key] == "succeeded"
            assert sum(r.url.path.endswith("/message:send") for r in fake.requests) == 1
        finally:
            await adapter.close()
    asyncio.run(exercise())


def test_capability_error_survives_async_runtime_context():
    from agent_runtime.capabilities.contracts import CapabilityError
    @asynccontextmanager
    async def boundary():
        yield
    async def exercise():
        with pytest.raises(CapabilityError) as caught:
            async with boundary():
                raise CapabilityError(code="CAPABILITY_TIMEOUT", message="超时", retryable=True)
        assert caught.value.code == "CAPABILITY_TIMEOUT"
    asyncio.run(exercise())


def test_auth_rejection_before_binding_is_definitive_but_after_binding_is_pending():
    from agent_runtime.capabilities.remote_a2a.capability import RemoteExecutionPending
    from agent_runtime.capabilities.remote_a2a.errors import RemoteA2AError
    async def exercise():
        fake, adapter, context, task_input, events, operations, output = remote_fixture()
        await adapter.initialize()
        try:
            fake.status_code = 401
            with pytest.raises(RemoteA2AError) as failed:
                await adapter.invoke_bound(context, None, task_input, sequencer=events)
            assert failed.value.code == "CAPABILITY_REMOTE_AUTH_FAILED"
            assert set(operations.rows.values()) == {"failed"}
            assert len(fake.tasks) == 0
        finally:
            await adapter.close()
        fake, adapter, context, task_input, events, operations, output = remote_fixture()
        await adapter.initialize()
        try:
            await adapter.invoke_bound(context, None, task_input, sequencer=events)
            fake.status_code = 401
            with pytest.raises(RemoteExecutionPending):
                await adapter.invoke_bound(context, None, task_input, sequencer=events)
            assert set(operations.rows.values()) == {"succeeded"}
        finally:
            await adapter.close()
    asyncio.run(exercise())


def test_auth_during_replay_without_binding_cannot_fail_previously_accepted_task():
    from agent_runtime.capabilities.remote_a2a.capability import RemoteExecutionPending
    async def exercise():
        fake, adapter, context, task_input, events, operations, output = remote_fixture()
        await adapter.initialize()
        try:
            fake.disconnect_after_accept = True
            with pytest.raises(RemoteExecutionPending):
                await adapter.invoke_bound(context, None, task_input, sequencer=events)
            fake.status_code = 401
            with pytest.raises(RemoteExecutionPending):
                await adapter.invoke_bound(context, None, task_input, sequencer=events)
            assert set(operations.rows.values()) == {"pending"}
            assert len(fake.tasks) == 1
        finally:
            await adapter.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("control,artifacts", [(False, False), (True, True)])
def test_plain_rejection_or_artifact_never_maps_to_scope_rejection(control, artifacts):
    from agent_runtime.capabilities.contracts import CapabilityError
    rejected_fake = FakeA2A(state="TASK_STATE_REJECTED")
    rejected_fake.out_of_scope = control
    async def exercise():
        fake, adapter, context, task_input, events, operations, output = remote_fixture(rejected_fake)
        await adapter.initialize()
        try:
            task = await adapter.acquire_task(context, task_input, sequencer=events)
            if artifacts:
                fake.tasks[task.id]["artifacts"] = [{"artifactId": "invalid", "parts": [{"text": "拒绝时不应输出"}]}]
            with pytest.raises(CapabilityError) as caught:
                await adapter.invoke_bound(context, None, task_input, sequencer=events)
            assert caught.value.code == "CAPABILITY_REMOTE_TASK_FAILED"
            assert output == []
        finally:
            await adapter.close()
    asyncio.run(exercise())


def test_remote_binding_cannot_bypass_controlled_repository():
    from datetime import UTC, datetime
    from agent_runtime.runtime.event_models import RuntimeEvent
    from agent_runtime.runtime.event_repository import PostgresRuntimeEventRepository, RuntimeEventPersistenceError
    from agent_runtime.runtime.event_schemas import RemoteTaskBoundPayload
    payload = RemoteTaskBoundPayload(invocation_id=uuid4(), capability_id="remote_test", remote_task_id="task",
        remote_context_id="context", initial_message_id=uuid4(), remote_agent_identity_hash="a" * 64, protocol_version="1.0")
    event = RuntimeEvent(event_id=uuid4(), run_id=uuid4(), seq=1, event_type="internal.remote_agent.task_bound",
        source="runtime.remote_agent", visibility="internal", payload=payload.model_dump(mode="json"), schema_version=1,
        durability="durable", created_at=datetime.now(UTC))
    with pytest.raises(RuntimeEventPersistenceError):
        PostgresRuntimeEventRepository._validate_durable_event(event, allow_stateful=False)


def test_remote_identity_hash_includes_normalized_card_source():
    from agent_runtime.capabilities.manifest import RemoteA2ATransport
    from agent_runtime.capabilities.remote_a2a.card import validate_card
    fake = FakeA2A()
    snapshots = []
    for source in ("https://agent.example/first/agent-card.json", "https://agent.example/second/agent-card.json"):
        _, snapshot = validate_card(fake.card, transport=RemoteA2ATransport.model_validate(remote_transport(url=source)), development_mode=True)
        snapshots.append(snapshot)
    assert snapshots[0].fingerprint != snapshots[1].fingerprint


@pytest.mark.parametrize("state", ["TASK_STATE_WORKING", "TASK_STATE_INPUT_REQUIRED"])
def test_unknown_and_input_required_keep_invocation_unresolved(state):
    from agent_runtime.capabilities.remote_a2a.capability import RemoteExecutionPending
    async def exercise():
        fake, adapter, context, task_input, events, operations, output = remote_fixture(FakeA2A(state=state))
        await adapter.initialize()
        try:
            if state == "TASK_STATE_WORKING":
                fake.disconnect_after_accept = True
            with pytest.raises(RemoteExecutionPending):
                await adapter.invoke_bound(context, None, task_input, sequencer=events)
            assert output == []
            assert not any(kind.endswith("task_terminal") for _, kind in events.events)
        finally:
            await adapter.close()
    asyncio.run(exercise())
