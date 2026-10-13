"""通用 Capability Invocation Gateway 的动态执行链路测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from agent_runtime.capabilities.contracts import (
    AgentResult,
    AgentResultMetadata,
    HealthResult,
)
from agent_runtime.capabilities.execution import CapabilityConcurrencyController
from agent_runtime.capabilities.manifest import CapabilityManifest
from agent_runtime.capabilities.persistence_models import (
    CapabilityTask,
    CapabilityTaskResolution,
)
from agent_runtime.capabilities.registry import (
    CapabilityRegistryEntry,
    RouterProjection,
)
from agent_runtime.graph.state import ParentState
from agent_runtime.runtime.agent_contract import AgentTextEvent


def _manifest(
    *,
    capability_id: str = "dynamic_chat",
    state_scope: str = "invocation",
) -> CapabilityManifest:
    """构造不依赖两个测试 ID 的严格 Manifest。"""

    return CapabilityManifest.model_validate(
        {
            "manifest_schema_version": 1,
            "capability_id": capability_id,
            "name": "动态测试能力",
            "description": "验证通用 Invocation Gateway。",
            "enabled": True,
            "entrypoint": "tests.fake_capability:create_capability",
            "version": "1.0.0",
            "state_scope": state_scope,
            "state_schema_version": "1",
            "compatible_state_schema_versions": [],
            "allow_degraded": False,
            "concurrency": {
                "mode": "unlimited",
                "max_concurrency": None,
                "acquire_timeout_seconds": 1.0,
            },
            "execution": {
                "timeout_seconds": 5.0,
                "cancel_grace_seconds": 0.1,
            },
            "recovery_policy": "automatic",
            "side_effect_policy": "none",
        }
    )


class _FakeCapability:
    def __init__(self, result: AgentResult) -> None:
        self.result = result
        self.agent_context = None

    async def initialize(self) -> None:
        return None

    async def health_check(self) -> HealthResult:
        return HealthResult(status="healthy", summary_code="READY")

    async def invoke(self, run_context, agent_context, task_input):
        self.agent_context = agent_context
        if self.result.status == "completed":
            run_context.events.emit(AgentTextEvent(text=self.result.content))
        assert isinstance(task_input.messages[-1], HumanMessage)
        return self.result


class _FakeRegistry:
    def __init__(self, entry: CapabilityRegistryEntry) -> None:
        self.entry = entry
        self.serviceable_calls = 0

    def get(self, capability_id: str):
        return self.entry if capability_id == self.entry.capability_id else None

    async def serviceable_router_projections(self):
        self.serviceable_calls += 1
        manifest = self.entry.manifest
        assert manifest is not None
        return (
            RouterProjection(
                capability_id=manifest.capability_id,
                name=manifest.name,
                description=manifest.description,
                enabled=True,
            ),
        )


class _FakePermissionService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def require_allowed(self, *, user_id: str, capability_id: str) -> None:
        self.calls.append((user_id, capability_id))


class _FakeInvocationService:
    def __init__(self, invocation_id: UUID) -> None:
        self.invocation_id = invocation_id
        self.started: list[dict[str, object]] = []
        self.completed: list[dict[str, object]] = []
        self.failed: list[dict[str, object]] = []
        self.cancelled: list[dict[str, object]] = []

    async def start(self, **kwargs):
        self.started.append(kwargs)
        return SimpleNamespace(
            invocation_id=self.invocation_id,
            recovered=False,
        )

    async def complete(self, **kwargs):
        self.completed.append(kwargs)

    async def fail(self, **kwargs):
        self.failed.append(kwargs)

    async def cancel(self, **kwargs):
        self.cancelled.append(kwargs)


class _FakeTaskService:
    def __init__(self, resolution: CapabilityTaskResolution) -> None:
        self.resolution = resolution
        self.resolve_calls: list[dict[str, object]] = []
        self.transitions: list[str] = []
        self.rollbacks: list[dict[str, object]] = []

    async def resolve(self, **kwargs):
        self.resolve_calls.append(kwargs)
        return self.resolution

    async def apply_transition(self, **kwargs):
        self.transitions.append(kwargs["task_transition"])
        return kwargs["task"]

    async def rollback_rejected_new(self, **kwargs):
        self.rollbacks.append(kwargs)


class _FakeOperationRepository:
    async def list_for_invocation(self, *, run_id: UUID, invocation_id: UUID):
        return ()


def _gateway_fixture(
    result: AgentResult,
    *,
    state_scope: str = "invocation",
):
    """构造可观察动态调用顺序的 Gateway 测试依赖。"""

    from agent_runtime.capabilities.invocation_gateway import (
        CapabilityInvocationGateway,
    )

    manifest = _manifest(state_scope=state_scope)
    capability = _FakeCapability(result)
    entry = CapabilityRegistryEntry(
        source=Path("dynamic/manifest.yaml"),
        status="active",
        manifest=manifest,
        capability=capability,
        concurrency_controller=CapabilityConcurrencyController(
            capability_id=manifest.capability_id,
            policy=manifest.concurrency,
        ),
        health=HealthResult(status="healthy", summary_code="READY"),
        diagnostic_code=None,
    )
    registry = _FakeRegistry(entry)
    permission = _FakePermissionService()
    invocation_id = uuid4()
    invocation_service = _FakeInvocationService(invocation_id)
    now = datetime.now(UTC)
    task = CapabilityTask(
        task_id=uuid4(),
        session_id=uuid4(),
        capability_id=manifest.capability_id,
        state_schema_version="1",
        status="active",
        last_run_id=None,
        last_invocation_id=None,
        created_at=now,
        updated_at=now,
        ended_at=None,
    )
    resolution = CapabilityTaskResolution(
        task=task,
        requested_action="new",
        resolved_action="new",
        previous_task_id=None,
        provisional=True,
        state_scope=state_scope,
    )
    task_service = _FakeTaskService(resolution)
    streamed: list[object] = []
    gateway = CapabilityInvocationGateway(
        registry=registry,
        permission_service=permission,
        task_repository=object(),
        operation_repository=_FakeOperationRepository(),
        child_checkpointer_provider=lambda _capability_id: MemorySaver(),
        sequencer_factory=lambda _run_id, _response_message_id: object(),
        invocation_service_factory=lambda _sequencer: invocation_service,
        task_service_factory=(
            lambda _sequencer, _invocation_service: task_service
        ),
        stream_writer_factory=lambda: streamed.append,
        now_factory=lambda: now,
    )
    return (
        gateway,
        capability,
        registry,
        permission,
        invocation_service,
        task_service,
        task,
        streamed,
    )


def _invoke_gateway(
    gateway,
    task: CapabilityTask,
    *,
    capability_id_metadata: str | None = None,
):
    run_id = uuid4()
    request_id = uuid4()
    input_message_id = uuid4()
    response_message_id = uuid4()

    async def never_cancelled() -> bool:
        return False

    state: ParentState = {
        "messages": [HumanMessage(content="动态请求")],
        "resolved_capability_id": "dynamic_chat",
        "task_action": "new",
        "rejected_capability_ids": [],
    }
    config = {
        "configurable": {
            "thread_id": str(task.session_id),
            "message_id": str(response_message_id),
        },
        "metadata": {
            "run_id": str(run_id),
            "request_id": str(request_id),
            "input_message_id": str(input_message_id),
            "response_message_id": str(response_message_id),
        },
    }
    if capability_id_metadata is not None:
        config["metadata"]["capability_id"] = capability_id_metadata
    return asyncio.run(
        gateway.invoke(
            state,
            config,
            user_id="runtime-user",
            cancellation_probe=never_cancelled,
        )
    )


def test_gateway_executes_dynamic_capability_and_persists_canonical_message() -> None:
    """Gateway 返回规范全文时必须保留 open Invocation 供 Parent 落盘后关闭。"""

    fixture = _gateway_fixture(
        AgentResult(
            status="completed",
            content="动态能力回答",
            metadata=AgentResultMetadata(task_transition="keep_active"),
        )
    )
    (
        gateway,
        capability,
        registry,
        permission,
        invocation_service,
        task_service,
        task,
        streamed,
    ) = fixture

    invocation = _invoke_gateway(gateway, task)

    assert invocation.result.status == "completed"
    assert invocation.message is not None
    assert invocation.message.text == "动态能力回答"
    assert invocation.message.additional_kwargs == {
        "runtime_status": "completed",
        "capability_id": "dynamic_chat",
    }
    assert registry.serviceable_calls == 1
    assert permission.calls == [("runtime-user", "dynamic_chat")]
    assert invocation_service.started[0]["capability_id"] == "dynamic_chat"
    assert invocation_service.completed == []
    assert task_service.transitions == ["keep_active"]
    assert capability.agent_context.task_id == task.task_id
    assert capability.agent_context.state_scope == "invocation"
    assert capability.agent_context.thread_id.endswith(
        f"invocation:{invocation_service.invocation_id}"
    )
    assert len(streamed) == 1
    assert streamed[0].text == "动态能力回答"


def test_gateway_rolls_back_provisional_task_on_clean_rejection() -> None:
    """new Task 的零副作用 OUT_OF_SCOPE 必须回滚并交给 Parent 有限回流。"""

    fixture = _gateway_fixture(
        AgentResult(
            status="rejected",
            content="",
            metadata=AgentResultMetadata(control_signal="OUT_OF_SCOPE"),
        )
    )
    gateway = fixture[0]
    invocation_service = fixture[4]
    task_service = fixture[5]
    task = fixture[6]
    streamed = fixture[7]

    invocation = _invoke_gateway(gateway, task)

    assert invocation.result.status == "rejected"
    assert invocation.result.control_signal == "OUT_OF_SCOPE"
    assert invocation.message is None
    assert len(task_service.rollbacks) == 1
    assert invocation_service.completed == []
    assert invocation_service.failed == []
    assert streamed == []


@pytest.mark.parametrize(
    ("state_scope", "thread_fragment"),
    [
        ("invocation", "invocation:"),
        ("run", "run:"),
        ("session", "task:"),
    ],
)
def test_gateway_applies_manifest_state_scope_without_business_branches(
    state_scope: str,
    thread_fragment: str,
) -> None:
    """同一通用 Gateway 必须仅按 Manifest 变化生成三种 State Scope。"""

    fixture = _gateway_fixture(
        AgentResult(status="completed", content="统一能力回答"),
        state_scope=state_scope,
    )
    gateway, capability, *_remaining, task, _streamed = fixture

    _invoke_gateway(gateway, task)

    assert capability.agent_context.state_scope == state_scope
    assert thread_fragment in capability.agent_context.thread_id


def test_gateway_rejects_regenerate_capability_metadata_mismatch() -> None:
    """Regenerate 固化的来源能力不得与 Parent Fork 状态发生漂移。"""

    fixture = _gateway_fixture(
        AgentResult(status="completed", content="不会执行")
    )
    gateway = fixture[0]
    invocation_service = fixture[4]
    task = fixture[6]

    with pytest.raises(ValueError, match="Capability 标识不一致"):
        _invoke_gateway(
            gateway,
            task,
            capability_id_metadata="other_capability",
        )

    assert invocation_service.started == []
