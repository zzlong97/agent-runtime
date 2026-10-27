"""Stage 3 Capability 崩溃恢复策略与稳定调用上下文测试。"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agent_runtime.capabilities.manifest import CapabilityManifest
from agent_runtime.capabilities.persistence_models import (
    CapabilityOperation,
    CapabilityTask,
)
from agent_runtime.runtime.event_models import RuntimeEvent


def _manifest(
    capability_id: str,
    *,
    recovery_policy: str,
    side_effect_policy: str,
) -> CapabilityManifest:
    """构造只包含当前恢复测试所需差异的严格 Manifest。"""

    return CapabilityManifest.model_validate(
        {
            "manifest_schema_version": 1,
            "capability_id": capability_id,
            "name": f"测试能力 {capability_id}",
            "description": "用于验证恢复策略，不包含真实业务调用。",
            "enabled": True,
            "transport": {"type": "local", "entrypoint": "tests.fake_capability:create_capability"},
            "version": "1.0.0",
            "state_scope": "session",
            "state_schema_version": "1",
            "compatible_state_schema_versions": [],
            "allow_degraded": False,
            "concurrency": {
                "mode": "unlimited",
                "max_concurrency": None,
                "acquire_timeout_seconds": 1.0,
            },
            "execution": {
                "timeout_seconds": 30.0,
                "cancel_grace_seconds": 1.0,
            },
            "recovery_policy": recovery_policy,
            "side_effect_policy": side_effect_policy,
        }
    )


def _started_event(
    *,
    run_id: UUID,
    invocation_id: UUID,
    capability_id: str,
) -> RuntimeEvent:
    """构造已持久化但尚无终态的 Invocation started 事件。"""

    return RuntimeEvent(
        event_id=uuid4(),
        run_id=run_id,
        seq=1,
        event_type="internal.capability.invocation.started",
        source="runtime.capability",
        visibility="internal",
        payload={
            "invocation_id": str(invocation_id),
            "capability_id": capability_id,
            "task_id": None,
            "requested_task_action": "continue",
            "effective_task_action": None,
            "state_scope": "session",
            "state_schema_version": None,
        },
        schema_version=1,
        durability="durable",
        created_at=datetime.now(UTC),
    )


class _EventRepository:
    def __init__(self, event: RuntimeEvent | None) -> None:
        self.event = event

    async def get_open_capability_invocation(
        self,
        *,
        run_id: UUID,
    ) -> RuntimeEvent | None:
        assert self.event is None or self.event.run_id == run_id
        return self.event


class _TaskRepository:
    def __init__(
        self,
        task: CapabilityTask | None,
        *,
        effective_action: str = "continue",
    ) -> None:
        self.task = task
        self.effective_action = effective_action

    async def get_by_invocation(
        self,
        *,
        run_id: UUID,
        invocation_id: UUID,
    ) -> CapabilityTask | None:
        assert self.task is None or self.task.last_run_id == run_id
        assert self.task is None or self.task.last_invocation_id == invocation_id
        return self.task

    async def get_resolution_action(
        self,
        *,
        run_id: UUID,
        task_id: UUID,
        requested_action: str,
    ) -> str:
        assert self.task is not None
        assert self.task.last_run_id == run_id
        assert self.task.task_id == task_id
        assert requested_action in {"continue", "new"}
        return self.effective_action


class _OperationRepository:
    def __init__(self, operations: tuple[CapabilityOperation, ...]) -> None:
        self.operations = operations

    async def list_for_invocation(
        self,
        *,
        run_id: UUID,
        invocation_id: UUID,
    ) -> tuple[CapabilityOperation, ...]:
        assert all(operation.run_id == run_id for operation in self.operations)
        assert all(
            operation.invocation_id == invocation_id
            for operation in self.operations
        )
        return self.operations


class _Registry:
    def __init__(self, *manifests: CapabilityManifest) -> None:
        self._manifests = {
            manifest.capability_id: manifest for manifest in manifests
        }

    def get(self, capability_id: str):
        manifest = self._manifests.get(capability_id)
        return None if manifest is None else SimpleNamespace(manifest=manifest)


def test_open_invocation_resolver_recovers_stable_task_and_operations() -> None:
    """重启后必须从 durable 事实恢复同一 Invocation、Task 和 Operation。"""

    from agent_runtime.capabilities.operation_ledger import (
        build_operation_idempotency_key,
    )
    from agent_runtime.capabilities.recovery import OpenInvocationResolver

    run_id = uuid4()
    invocation_id = uuid4()
    task_id = uuid4()
    now = datetime.now(UTC)
    task = CapabilityTask(
        task_id=task_id,
        session_id=uuid4(),
        capability_id="mail_agent",
        state_schema_version="1",
        status="active",
        last_run_id=run_id,
        last_invocation_id=invocation_id,
        created_at=now,
        updated_at=now,
        ended_at=None,
    )
    operation = CapabilityOperation(
        operation_id=uuid4(),
        run_id=run_id,
        invocation_id=invocation_id,
        task_id=task_id,
        capability_id="mail_agent",
        operation_key="send-email",
        idempotency_key=build_operation_idempotency_key(
            run_id=run_id,
            invocation_id=invocation_id,
            operation_key="send-email",
        ),
        status="pending",
        created_at=now,
        updated_at=now,
    )
    resolver = OpenInvocationResolver(
        event_repository=_EventRepository(
            _started_event(
                run_id=run_id,
                invocation_id=invocation_id,
                capability_id="mail_agent",
            )
        ),
        task_repository=_TaskRepository(task, effective_action="new"),
        operation_repository=_OperationRepository((operation,)),
    )

    context = asyncio.run(resolver.resolve(run_id=run_id))

    assert context is not None
    assert context.invocation_id == invocation_id
    assert context.task == task
    assert context.effective_task_action == "new"
    assert context.operations == (operation,)


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
def test_open_invocation_resolver_accepts_terminal_task_projection(
    terminal_status: str,
) -> None:
    """Task 终态先提交时仍应恢复同一 open Invocation。"""

    from agent_runtime.capabilities.recovery import OpenInvocationResolver

    run_id = uuid4()
    invocation_id = uuid4()
    now = datetime.now(UTC)
    task = CapabilityTask(
        task_id=uuid4(),
        session_id=uuid4(),
        capability_id="terminal_agent",
        state_schema_version="1",
        status=terminal_status,
        last_run_id=run_id,
        last_invocation_id=invocation_id,
        created_at=now,
        updated_at=now,
        ended_at=now,
    )
    resolver = OpenInvocationResolver(
        event_repository=_EventRepository(
            _started_event(
                run_id=run_id,
                invocation_id=invocation_id,
                capability_id="terminal_agent",
            )
        ),
        task_repository=_TaskRepository(task),
        operation_repository=_OperationRepository(()),
    )

    context = asyncio.run(resolver.resolve(run_id=run_id))

    assert context is not None
    assert context.task == task


@pytest.mark.parametrize(
    ("recovery_policy", "side_effect_policy", "expected_action"),
    [
        ("automatic", "none", "automatic"),
        ("automatic", "idempotent", "automatic"),
        ("manual", "none", "manual_fail"),
        ("manual", "idempotent", "manual_fail"),
        ("manual", "unsafe", "manual_fail"),
    ],
)
def test_recovery_policy_evaluator_uses_manifest_matrix(
    recovery_policy: str,
    side_effect_policy: str,
    expected_action: str,
) -> None:
    """恢复动作必须只由严格 Manifest 的恢复矩阵决定。"""

    from agent_runtime.capabilities.recovery import (
        CapabilityRecoveryPolicyEvaluator,
        OpenInvocationResolver,
    )

    capability_id = "matrix_agent"
    manifest = _manifest(
        capability_id,
        recovery_policy=recovery_policy,
        side_effect_policy=side_effect_policy,
    )
    evaluator = CapabilityRecoveryPolicyEvaluator(
        registry=_Registry(manifest),
        invocation_resolver=OpenInvocationResolver(
            event_repository=_EventRepository(None),
            task_repository=_TaskRepository(None),
            operation_repository=_OperationRepository(()),
        ),
    )

    decision = asyncio.run(
        evaluator.evaluate(
            run_id=uuid4(),
            checkpoint_capability_id=capability_id,
        )
    )

    assert decision.action == expected_action
    assert decision.capability_id == capability_id


def test_recovery_evaluator_rejects_checkpoint_and_open_invocation_mismatch() -> None:
    """Checkpoint 与 durable open Invocation 指向不同能力时必须安全拒绝。"""

    from agent_runtime.capabilities.recovery import (
        CapabilityRecoveryError,
        CapabilityRecoveryPolicyEvaluator,
        OpenInvocationResolver,
    )

    run_id = uuid4()
    resolver = OpenInvocationResolver(
        event_repository=_EventRepository(
            _started_event(
                run_id=run_id,
                invocation_id=uuid4(),
                capability_id="mail_agent",
            )
        ),
        task_repository=_TaskRepository(None),
        operation_repository=_OperationRepository(()),
    )
    evaluator = CapabilityRecoveryPolicyEvaluator(
        registry=_Registry(
            _manifest(
                "mail_agent",
                recovery_policy="automatic",
                side_effect_policy="idempotent",
            ),
            _manifest(
                "chat_agent",
                recovery_policy="automatic",
                side_effect_policy="none",
            ),
        ),
        invocation_resolver=resolver,
    )

    with pytest.raises(
        CapabilityRecoveryError,
        match="Checkpoint 与 open Invocation 的 Capability 不一致",
    ):
        asyncio.run(
            evaluator.evaluate(
                run_id=run_id,
                checkpoint_capability_id="chat_agent",
            )
        )


def test_recovery_evaluator_allows_pre_capability_automatic_recovery() -> None:
    """尚未选出 Capability 的崩溃继续沿用 S2.5 自动恢复。"""

    from agent_runtime.capabilities.recovery import (
        CapabilityRecoveryPolicyEvaluator,
        OpenInvocationResolver,
    )

    evaluator = CapabilityRecoveryPolicyEvaluator(
        registry=_Registry(),
        invocation_resolver=OpenInvocationResolver(
            event_repository=_EventRepository(None),
            task_repository=_TaskRepository(None),
            operation_repository=_OperationRepository(()),
        ),
    )

    decision = asyncio.run(
        evaluator.evaluate(
            run_id=uuid4(),
            checkpoint_capability_id=None,
        )
    )

    assert decision.action == "automatic"
    assert decision.capability_id is None
    assert decision.invocation is None


def test_manual_recovery_safely_fails_even_if_none_policy_has_operation() -> None:
    """manual 必须优先安全失败，不得因异常 Operation 事实转为自动重放。"""

    from agent_runtime.capabilities.operation_ledger import (
        build_operation_idempotency_key,
    )
    from agent_runtime.capabilities.recovery import (
        CapabilityRecoveryPolicyEvaluator,
        OpenInvocationResolver,
    )

    run_id = uuid4()
    invocation_id = uuid4()
    task_id = uuid4()
    now = datetime.now(UTC)
    task = CapabilityTask(
        task_id=task_id,
        session_id=uuid4(),
        capability_id="manual_agent",
        state_schema_version="1",
        status="active",
        last_run_id=run_id,
        last_invocation_id=invocation_id,
        created_at=now,
        updated_at=now,
        ended_at=None,
    )
    operation = CapabilityOperation(
        operation_id=uuid4(),
        run_id=run_id,
        invocation_id=invocation_id,
        task_id=task_id,
        capability_id="manual_agent",
        operation_key="unexpected-operation",
        idempotency_key=build_operation_idempotency_key(
            run_id=run_id,
            invocation_id=invocation_id,
            operation_key="unexpected-operation",
        ),
        status="pending",
        created_at=now,
        updated_at=now,
    )
    evaluator = CapabilityRecoveryPolicyEvaluator(
        registry=_Registry(
            _manifest(
                "manual_agent",
                recovery_policy="manual",
                side_effect_policy="none",
            )
        ),
        invocation_resolver=OpenInvocationResolver(
            event_repository=_EventRepository(
                _started_event(
                    run_id=run_id,
                    invocation_id=invocation_id,
                    capability_id="manual_agent",
                )
            ),
            task_repository=_TaskRepository(task),
            operation_repository=_OperationRepository((operation,)),
        ),
    )

    decision = asyncio.run(
        evaluator.evaluate(
            run_id=run_id,
            checkpoint_capability_id="manual_agent",
        )
    )

    assert decision.action == "manual_fail"
    assert decision.invocation is not None
    assert decision.invocation.operations == (operation,)


def test_automatic_unsafe_manifest_remains_invalid() -> None:
    """S3-10 不得绕过加载期的 automatic + unsafe 隔离。"""

    with pytest.raises(ValidationError):
        _manifest(
            "unsafe_agent",
            recovery_policy="automatic",
            side_effect_policy="unsafe",
        )
