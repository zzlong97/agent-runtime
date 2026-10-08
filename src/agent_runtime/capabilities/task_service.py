"""Capability Task 解析、终态和无副作用拒绝回滚服务。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel

from agent_runtime.capabilities.invocation_service import (
    CapabilityInvocationService,
)
from agent_runtime.capabilities.manifest import CapabilityManifest
from agent_runtime.capabilities.persistence import (
    CapabilityTaskContractViolationError,
    CapabilityTaskRepository,
)
from agent_runtime.capabilities.persistence_models import (
    CapabilityTask,
    CapabilityTaskAction,
    CapabilityTaskResolution,
)
from agent_runtime.capabilities.state_scope import StateCompatibilityPolicy
from agent_runtime.core.logging import log_business_event
from agent_runtime.runtime.event_models import RuntimeEvent, RuntimeEventDraft
from agent_runtime.runtime.event_schemas import (
    CapabilityTaskContinueDegradedPayload,
    CapabilityTaskCreatedPayload,
    CapabilityTaskCurrentChangedPayload,
    CapabilityTaskRejectedRolledBackPayload,
    CapabilityTaskTerminalPayload,
    InvocationCompletedPayload,
)
from agent_runtime.runtime.sequencer import RunSequencer

logger = logging.getLogger(__name__)


class ChildCheckpointStore(Protocol):
    """Task 拒绝回滚只需要观察和删除整个 Child thread。"""

    async def has_checkpoint(self, thread_id: str) -> bool:
        """判断指定线程是否已经持久化任何业务 State。"""

        ...

    async def delete_thread(self, thread_id: str) -> None:
        """幂等删除指定临时 Child thread 的全部 Checkpoint。"""

        ...


class CheckpointerThreadStore:
    """把 LangGraph Checkpointer 收敛为拒绝回滚所需的最小接口。"""

    def __init__(self, checkpointer: object) -> None:
        self._checkpointer = checkpointer

    async def has_checkpoint(self, thread_id: str) -> bool:
        """只检查 Checkpoint 是否存在，不读取 Capability 私有 payload。"""

        get_tuple = getattr(self._checkpointer, "aget_tuple", None)
        if not callable(get_tuple):
            raise TypeError("Child Checkpointer 必须实现 aget_tuple")
        checkpoint = await get_tuple(_thread_config(thread_id))
        return checkpoint is not None

    async def delete_thread(self, thread_id: str) -> None:
        """调用 Checkpointer 的线程级幂等删除入口。"""

        delete_thread = getattr(self._checkpointer, "adelete_thread", None)
        if not callable(delete_thread):
            raise TypeError("Child Checkpointer 必须实现 adelete_thread")
        await delete_thread(thread_id)


class CapabilityTaskService:
    """只编排 S3-05 已确认的 Task 生命周期，不调用 Capability。"""

    def __init__(
        self,
        *,
        repository: CapabilityTaskRepository,
        sequencer: RunSequencer,
        invocation_service: CapabilityInvocationService,
        task_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._repository = repository
        self._sequencer = sequencer
        self._invocation_service = invocation_service
        self._task_id_factory = task_id_factory

    async def resolve(
        self,
        *,
        manifest: CapabilityManifest,
        session_id: UUID,
        run_id: UUID,
        invocation_id: UUID,
        task_action: CapabilityTaskAction,
        updated_at: datetime,
    ) -> CapabilityTaskResolution:
        """原子解析 new/continue，并记录新建或降级所需内部事件。"""

        candidate_task_id = self._task_id_factory()
        drafts = self._resolution_drafts(
            manifest=manifest,
            task_id=candidate_task_id,
            task_action=task_action,
            created_at=updated_at,
        )
        policy = StateCompatibilityPolicy.from_manifest(manifest)

        async def commit(
            events: tuple[RuntimeEvent, ...],
        ) -> CapabilityTaskResolution:
            return await self._repository.resolve_for_invocation(
                session_id=session_id,
                capability_id=manifest.capability_id,
                requested_action=task_action,
                state_scope=manifest.state_scope,
                state_schema_version=manifest.state_schema_version,
                compatible_state_schema_versions=policy.allowed_versions,
                run_id=run_id,
                invocation_id=invocation_id,
                candidate_task_id=candidate_task_id,
                updated_at=updated_at,
                events=events,
            )

        return await self._sequencer.commit_durable_internal_batch(
            drafts=drafts,
            commit=commit,
        )

    async def apply_transition(
        self,
        *,
        task: CapabilityTask,
        task_transition: Literal[
            "keep_active", "completed", "failed", "cancelled"
        ],
        run_id: UUID,
        invocation_id: UUID,
        updated_at: datetime,
    ) -> CapabilityTask:
        """只应用 AgentResult 明确提出的 Task 意图；keep_active 不写终态。"""

        if task_transition == "keep_active":
            log_business_event(
                logger,
                "Capability Task保持活动态",
                session_id=task.session_id,
                task_id=task.task_id,
                capability_id=task.capability_id,
                run_id=run_id,
                invocation_id=invocation_id,
                status=task.status,
            )
            return task
        payload = CapabilityTaskTerminalPayload(
            task_id=task.task_id,
            capability_id=task.capability_id,
            status=task_transition,
        )
        draft = self._internal_draft(
            event_type=f"internal.capability.task.{task_transition}",
            payload=payload,
            created_at=updated_at,
        )

        async def commit(events: tuple[RuntimeEvent, ...]) -> CapabilityTask:
            return await self._repository.transition_for_invocation(
                task_id=task.task_id,
                status=task_transition,
                run_id=run_id,
                invocation_id=invocation_id,
                updated_at=updated_at,
                event=events[0],
            )

        result = await self._sequencer.commit_durable_internal_batch(
            drafts=(draft,),
            commit=commit,
        )
        log_business_event(
            logger,
            "Capability Task终态应用完成",
            session_id=result.session_id,
            task_id=result.task_id,
            capability_id=result.capability_id,
            run_id=run_id,
            invocation_id=invocation_id,
            status=result.status,
        )
        return result

    async def rollback_rejected_new(
        self,
        *,
        resolution: CapabilityTaskResolution,
        run_id: UUID,
        invocation_id: UUID,
        thread_id: str,
        public_output_emitted: bool,
        checkpoint_store: ChildCheckpointStore,
        updated_at: datetime,
    ) -> tuple[RuntimeEvent, RuntimeEvent]:
        """仅在零输出、零 Operation、零 Checkpoint 时回滚 provisional Task。"""

        if not resolution.provisional:
            raise ValueError("只有 provisional Task 可以执行 OUT_OF_SCOPE 回滚")
        log_business_event(
            logger,
            "Capability provisional Task拒绝回滚开始",
            session_id=resolution.task.session_id,
            task_id=resolution.task.task_id,
            capability_id=resolution.task.capability_id,
            run_id=run_id,
            invocation_id=invocation_id,
        )
        checkpoint_exists = await checkpoint_store.has_checkpoint(thread_id)
        if public_output_emitted or checkpoint_exists:
            reason = "PUBLIC_OUTPUT" if public_output_emitted else "CHILD_CHECKPOINT"
            await self._fail_contract_violation(
                resolution=resolution,
                invocation_id=invocation_id,
                reason=reason,
                updated_at=updated_at,
            )

        task_event_draft = self._internal_draft(
            event_type="internal.capability.task.rejected_rolled_back",
            payload=CapabilityTaskRejectedRolledBackPayload(
                task_id=resolution.task.task_id,
                capability_id=resolution.task.capability_id,
            ),
            created_at=updated_at,
        )
        invocation_event_draft = self._internal_draft(
            event_type="internal.capability.invocation.completed",
            payload=InvocationCompletedPayload(
                invocation_id=invocation_id,
                capability_id=resolution.task.capability_id,
                task_id=resolution.task.task_id,
                requested_task_action=resolution.requested_action,
                effective_task_action=resolution.resolved_action,
                state_scope=resolution.state_scope,
                state_schema_version=resolution.task.state_schema_version,
                outcome="rejected",
            ),
            created_at=updated_at,
        )

        async def commit(
            events: tuple[RuntimeEvent, ...],
        ) -> tuple[RuntimeEvent, RuntimeEvent]:
            return await self._repository.rollback_rejected_new(
                resolution=resolution,
                run_id=run_id,
                invocation_id=invocation_id,
                task_event=events[0],
                invocation_event=events[1],
                updated_at=updated_at,
            )

        try:
            stored = await self._sequencer.commit_durable_internal_batch(
                drafts=(task_event_draft, invocation_event_draft),
                commit=commit,
            )
        except CapabilityTaskContractViolationError:
            await self._fail_contract_violation(
                resolution=resolution,
                invocation_id=invocation_id,
                reason="OPERATION",
                updated_at=updated_at,
            )
            raise AssertionError("不可达")
        await checkpoint_store.delete_thread(thread_id)
        log_business_event(
            logger,
            "Capability provisional Task拒绝回滚完成",
            session_id=resolution.task.session_id,
            task_id=resolution.task.task_id,
            capability_id=resolution.task.capability_id,
            run_id=run_id,
            invocation_id=invocation_id,
        )
        return stored

    async def _fail_contract_violation(
        self,
        *,
        resolution: CapabilityTaskResolution,
        invocation_id: UUID,
        reason: str,
        updated_at: datetime,
    ) -> None:
        """持久关闭违规 Invocation，并以稳定执行失败阻止 Task 回滚。"""

        await self._invocation_service.fail(
            invocation_id=invocation_id,
            capability_id=resolution.task.capability_id,
            task_id=resolution.task.task_id,
            requested_task_action=resolution.requested_action,
            effective_task_action=resolution.resolved_action,
            state_scope=resolution.state_scope,
            state_schema_version=resolution.task.state_schema_version,
            error_code="CAPABILITY_EXECUTION_FAILED",
            created_at=updated_at,
        )
        log_business_event(
            logger,
            "Capability拒绝回滚被拒绝",
            session_id=resolution.task.session_id,
            task_id=resolution.task.task_id,
            capability_id=resolution.task.capability_id,
            invocation_id=invocation_id,
            reason=reason,
            error_code="CAPABILITY_EXECUTION_FAILED",
        )
        raise CapabilityTaskContractViolationError(
            code="CAPABILITY_EXECUTION_FAILED",
            message="Capability 在范围拒绝前已经产生业务输出或状态",
            status_code=409,
            retryable=False,
        )

    def _resolution_drafts(
        self,
        *,
        manifest: CapabilityManifest,
        task_id: UUID,
        task_action: CapabilityTaskAction,
        created_at: datetime,
    ) -> tuple[RuntimeEventDraft, ...]:
        """预分配可能需要的 Task 事件；未使用序号按既定规则形成合法缺号。"""

        drafts = [
            self._internal_draft(
                event_type="internal.capability.task.created",
                payload=CapabilityTaskCreatedPayload(
                    task_id=task_id,
                    capability_id=manifest.capability_id,
                    state_schema_version=manifest.state_schema_version,
                ),
                created_at=created_at,
            ),
            self._internal_draft(
                event_type="internal.capability.task.current_changed",
                payload=CapabilityTaskCurrentChangedPayload(
                    task_id=task_id,
                    capability_id=manifest.capability_id,
                    previous_task_id=None,
                ),
                created_at=created_at,
            ),
        ]
        if task_action == "continue":
            drafts.append(
                self._internal_draft(
                    event_type=(
                        "internal.capability.task.continue_degraded_to_new"
                    ),
                    payload=CapabilityTaskContinueDegradedPayload(
                        task_id=task_id,
                        capability_id=manifest.capability_id,
                    ),
                    created_at=created_at,
                )
            )
        return tuple(drafts)

    @staticmethod
    def _internal_draft(
        *,
        event_type: str,
        payload: BaseModel,
        created_at: datetime,
    ) -> RuntimeEventDraft:
        """构造不进入 Redis 或公开 SSE 的固定内部 durable 事件。"""

        return RuntimeEventDraft(
            event_type=event_type,
            source="runtime.capability",
            visibility="internal",
            payload=payload,
            schema_version=1,
            durability="durable",
            created_at=created_at,
        )


def _thread_config(thread_id: str) -> dict[str, dict[str, str]]:
    """构造只包含公开线程标识和空 namespace 的 LangGraph 配置。"""

    if type(thread_id) is not str or not thread_id.strip():
        raise ValueError("Child thread_id 不能为空")
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }
