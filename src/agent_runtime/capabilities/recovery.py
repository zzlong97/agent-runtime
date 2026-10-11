"""Stage 3 Capability 恢复策略判定与 durable 调用上下文解析。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Literal, Protocol
from uuid import UUID

from agent_runtime.capabilities.manifest import (
    ManifestCapabilityId,
    validate_capability_id,
)
from agent_runtime.capabilities.operation_ledger import (
    build_operation_idempotency_key,
)
from agent_runtime.capabilities.persistence_models import (
    CapabilityOperation,
    CapabilityTask,
    CapabilityTaskAction,
    CapabilityStateScope,
)
from agent_runtime.capabilities.registry import CapabilityRegistry
from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event
from agent_runtime.runtime.event_models import RuntimeEvent
from agent_runtime.runtime.event_schemas import InvocationStartedPayload

logger = logging.getLogger(__name__)

RecoveryAction = Literal["automatic", "manual_fail"]


class CapabilityRecoveryError(ApplicationError):
    """Capability 恢复上下文不完整、冲突或不满足安全策略。"""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        invocation: OpenInvocationRecoveryContext | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            status_code=409,
            retryable=False,
        )
        self.invocation = invocation


class OpenInvocationEventStore(Protocol):
    """读取单个 Run 唯一 open Invocation 的最小接口。"""

    async def get_open_capability_invocation(
        self,
        *,
        run_id: UUID,
    ) -> RuntimeEvent | None:
        """返回尚无终态事件的 Invocation started 事件。"""

        ...


class RecoveryTaskStore(Protocol):
    """按稳定调用身份读取 Task 投影的最小接口。"""

    async def get_by_invocation(
        self,
        *,
        run_id: UUID,
        invocation_id: UUID,
    ) -> CapabilityTask | None:
        """返回最后一次执行属于指定调用的 Task。"""

        ...

    async def get_resolution_action(
        self,
        *,
        run_id: UUID,
        task_id: UUID,
        requested_action: CapabilityTaskAction,
    ) -> CapabilityTaskAction:
        """从 durable Task 事件还原本次调用实际采用的动作。"""

        ...


class RecoveryOperationStore(Protocol):
    """读取指定 Invocation 全部 Operation 的最小接口。"""

    async def list_for_invocation(
        self,
        *,
        run_id: UUID,
        invocation_id: UUID,
    ) -> tuple[CapabilityOperation, ...]:
        """按稳定顺序返回 Operation，不包含业务响应。"""

        ...


@dataclass(frozen=True, slots=True)
class OpenInvocationRecoveryContext:
    """跨进程恢复出的唯一 open Invocation 及其持久关联事实。"""

    run_id: UUID
    invocation_id: UUID
    capability_id: ManifestCapabilityId
    requested_task_action: CapabilityTaskAction
    effective_task_action: CapabilityTaskAction | None
    state_scope: CapabilityStateScope
    task: CapabilityTask | None
    operations: tuple[CapabilityOperation, ...]
    started_event: RuntimeEvent


@dataclass(frozen=True, slots=True)
class CapabilityRecoveryDecision:
    """Checkpoint 对账完成后可执行的唯一恢复动作。"""

    action: RecoveryAction
    capability_id: ManifestCapabilityId | None
    recovery_policy: Literal["automatic", "manual"] | None
    side_effect_policy: Literal["none", "idempotent", "unsafe"] | None
    invocation: OpenInvocationRecoveryContext | None


class OpenInvocationResolver:
    """从 RuntimeEvent、Task 和 Operation 恢复稳定调用身份。"""

    def __init__(
        self,
        *,
        event_repository: OpenInvocationEventStore,
        task_repository: RecoveryTaskStore,
        operation_repository: RecoveryOperationStore,
    ) -> None:
        self._event_repository = event_repository
        self._task_repository = task_repository
        self._operation_repository = operation_repository

    async def resolve(
        self,
        *,
        run_id: UUID,
    ) -> OpenInvocationRecoveryContext | None:
        """读取唯一 open started 事件；不存在表示尚未进入 Capability。"""

        started_at = perf_counter()
        log_business_event(logger, "Capability恢复调用上下文查询开始", run_id=run_id)
        event = await self._event_repository.get_open_capability_invocation(
            run_id=run_id
        )
        if event is None:
            log_business_event(
                logger,
                "Capability恢复调用上下文查询完成",
                run_id=run_id,
                invocation_found=False,
                duration_ms=_elapsed_ms(started_at),
            )
            return None
        try:
            payload = InvocationStartedPayload.model_validate(event.payload)
        except Exception as error:
            raise _recovery_error(
                code="CAPABILITY_RECOVERY_INVOCATION_INVALID",
                message="open Invocation started 事件不符合固定 Schema",
            ) from error
        if event.run_id != run_id:
            raise _recovery_error(
                code="CAPABILITY_RECOVERY_INVOCATION_INVALID",
                message="open Invocation 与目标 Run 不匹配",
            )
        invocation_id = UUID(str(payload.invocation_id))
        capability_id = validate_capability_id(payload.capability_id)
        task = await self._task_repository.get_by_invocation(
            run_id=run_id,
            invocation_id=invocation_id,
        )
        effective_task_action = (
            None
            if task is None
            else await self._task_repository.get_resolution_action(
                run_id=run_id,
                task_id=task.task_id,
                requested_action=payload.requested_task_action,
            )
        )
        operations = await self._operation_repository.list_for_invocation(
            run_id=run_id,
            invocation_id=invocation_id,
        )
        self._validate_ownership(
            run_id=run_id,
            invocation_id=invocation_id,
            capability_id=capability_id,
            task=task,
            operations=operations,
        )
        context = OpenInvocationRecoveryContext(
            run_id=run_id,
            invocation_id=invocation_id,
            capability_id=capability_id,
            requested_task_action=payload.requested_task_action,
            effective_task_action=effective_task_action,
            state_scope=payload.state_scope,
            task=task,
            operations=operations,
            started_event=event,
        )
        log_business_event(
            logger,
            "Capability恢复调用上下文查询完成",
            run_id=run_id,
            invocation_id=invocation_id,
            task_id=None if task is None else task.task_id,
            capability_id=capability_id,
            invocation_found=True,
            operation_count=len(operations),
            duration_ms=_elapsed_ms(started_at),
        )
        return context

    @staticmethod
    def _validate_ownership(
        *,
        run_id: UUID,
        invocation_id: UUID,
        capability_id: ManifestCapabilityId,
        task: CapabilityTask | None,
        operations: tuple[CapabilityOperation, ...],
    ) -> None:
        """保证恢复出的 Task、Operation 全部属于同一持久调用。"""

        if task is not None and (
            task.capability_id != capability_id
            or task.last_run_id != run_id
            or task.last_invocation_id != invocation_id
        ):
            raise _recovery_error(
                code="CAPABILITY_RECOVERY_TASK_MISMATCH",
                message="Capability Task 与 open Invocation 归属不一致",
            )
        for operation in operations:
            if (
                task is None
                or operation.run_id != run_id
                or operation.invocation_id != invocation_id
                or operation.task_id != task.task_id
                or operation.capability_id != capability_id
                or operation.idempotency_key
                != build_operation_idempotency_key(
                    run_id=run_id,
                    invocation_id=invocation_id,
                    operation_key=operation.operation_key,
                )
            ):
                raise _recovery_error(
                    code="CAPABILITY_RECOVERY_OPERATION_MISMATCH",
                    message="Capability Operation 与 open Invocation 归属不一致",
                )


class CapabilityRecoveryPolicyEvaluator:
    """在 Checkpoint-first 对账后按严格 Manifest 决定是否允许重放。"""

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        invocation_resolver: OpenInvocationResolver,
    ) -> None:
        self._registry = registry
        self._invocation_resolver = invocation_resolver

    async def evaluate(
        self,
        *,
        run_id: UUID,
        checkpoint_capability_id: object | None,
    ) -> CapabilityRecoveryDecision:
        """返回自动恢复或 manual 安全失败，不执行 Capability。"""

        started_at = perf_counter()
        invocation = await self._invocation_resolver.resolve(run_id=run_id)
        try:
            checkpoint_id = (
                None
                if checkpoint_capability_id is None
                else validate_capability_id(checkpoint_capability_id)
            )
        except (TypeError, ValueError) as error:
            raise _recovery_error(
                code="CAPABILITY_RECOVERY_CONTEXT_INVALID",
                message="Checkpoint 的 Capability 标识不合法",
                invocation=invocation,
            ) from error
        if (
            checkpoint_id is not None
            and invocation is not None
            and checkpoint_id != invocation.capability_id
        ):
            raise _recovery_error(
                code="CAPABILITY_RECOVERY_CONTEXT_MISMATCH",
                message="Checkpoint 与 open Invocation 的 Capability 不一致",
                invocation=invocation,
            )
        capability_id = (
            invocation.capability_id if invocation is not None else checkpoint_id
        )
        if capability_id is None:
            return CapabilityRecoveryDecision(
                action="automatic",
                capability_id=None,
                recovery_policy=None,
                side_effect_policy=None,
                invocation=None,
            )
        entry = self._registry.get(capability_id)
        if entry is None or entry.manifest is None:
            raise _recovery_error(
                code="CAPABILITY_RECOVERY_MANIFEST_UNAVAILABLE",
                message="Capability 恢复所需 Manifest 不可用",
                invocation=invocation,
            )
        manifest = entry.manifest
        if (
            manifest.recovery_policy == "automatic"
            and manifest.side_effect_policy == "unsafe"
        ):
            raise _recovery_error(
                code="CAPABILITY_RECOVERY_POLICY_INVALID",
                message="automatic 恢复策略不能与 unsafe 副作用策略组合",
                invocation=invocation,
            )
        if (
            manifest.recovery_policy == "automatic"
            and manifest.side_effect_policy == "none"
            and invocation is not None
            and invocation.operations
        ):
            raise _recovery_error(
                code="CAPABILITY_RECOVERY_OPERATION_UNEXPECTED",
                message="无副作用 Capability 不允许存在 Operation 记录",
                invocation=invocation,
            )
        action: RecoveryAction = (
            "manual_fail"
            if manifest.recovery_policy == "manual"
            else "automatic"
        )
        decision = CapabilityRecoveryDecision(
            action=action,
            capability_id=capability_id,
            recovery_policy=manifest.recovery_policy,
            side_effect_policy=manifest.side_effect_policy,
            invocation=invocation,
        )
        log_business_event(
            logger,
            "Capability恢复策略判定完成",
            run_id=run_id,
            invocation_id=(
                None if invocation is None else invocation.invocation_id
            ),
            task_id=(
                None
                if invocation is None or invocation.task is None
                else invocation.task.task_id
            ),
            capability_id=capability_id,
            recovery_policy=manifest.recovery_policy,
            side_effect_policy=manifest.side_effect_policy,
            recovery_action=action,
            duration_ms=_elapsed_ms(started_at),
        )
        return decision


def _recovery_error(
    *,
    code: str,
    message: str,
    invocation: OpenInvocationRecoveryContext | None = None,
) -> CapabilityRecoveryError:
    """构造不暴露持久化 payload 的稳定恢复错误。"""

    return CapabilityRecoveryError(
        code=code,
        message=message,
        invocation=invocation,
    )


def _elapsed_ms(started_at: float) -> float:
    """返回保留两位小数的毫秒耗时。"""

    return round((perf_counter() - started_at) * 1000, 2)
