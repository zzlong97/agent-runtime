"""Capability Invocation 的唯一 open 生命周期服务。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Literal
from uuid import UUID, uuid4

from agent_runtime.capabilities.state_scope import StateScope
from agent_runtime.core.logging import log_business_event
from agent_runtime.runtime.event_models import RuntimeEvent, RuntimeEventDraft
from agent_runtime.runtime.event_schemas import (
    InvocationCancelledPayload,
    InvocationCompletedPayload,
    InvocationFailedPayload,
    InvocationStartedPayload,
)
from agent_runtime.runtime.sequencer import RunSequencer

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CapabilityInvocation:
    """从 durable started 事件恢复出的当前调用身份。"""

    invocation_id: UUID
    capability_id: str
    task_action: Literal["continue", "new"]
    state_scope: StateScope
    started_event: RuntimeEvent
    recovered: bool


class CapabilityInvocationService:
    """通过 Run Sequencer 和 PostgreSQL 行锁维护 Invocation 生命周期。"""

    def __init__(
        self,
        *,
        sequencer: RunSequencer,
        invocation_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._sequencer = sequencer
        self._invocation_id_factory = invocation_id_factory

    async def start(
        self,
        *,
        capability_id: str,
        task_action: Literal["continue", "new"],
        state_scope: StateScope,
        created_at: datetime,
    ) -> CapabilityInvocation:
        """创建新 Invocation；已有 open started 时复用其稳定身份。"""

        started_at = perf_counter()
        proposed_id = self._invocation_id_factory()
        commit = await self._sequencer.begin_capability_invocation(
            RuntimeEventDraft(
                event_type="internal.capability.invocation.started",
                source="runtime.capability",
                visibility="internal",
                payload=InvocationStartedPayload(
                    invocation_id=proposed_id,
                    capability_id=capability_id,
                    task_id=None,
                    requested_task_action=task_action,
                    effective_task_action=None,
                    state_scope=state_scope,
                    state_schema_version=None,
                ),
                schema_version=1,
                durability="durable",
                created_at=created_at,
            )
        )
        payload = InvocationStartedPayload.model_validate(commit.event.payload)
        result = CapabilityInvocation(
            invocation_id=payload.invocation_id,
            capability_id=payload.capability_id,
            task_action=payload.requested_task_action,
            state_scope=payload.state_scope,
            started_event=commit.event,
            recovered=commit.recovered,
        )
        log_business_event(
            logger,
            "Capability Invocation恢复" if result.recovered else "Capability Invocation开始",
            run_id=result.started_event.run_id,
            invocation_id=result.invocation_id,
            capability_id=result.capability_id,
            task_action=result.task_action,
            state_scope=result.state_scope,
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        )
        return result

    async def complete(
        self,
        *,
        invocation_id: UUID,
        capability_id: str,
        task_id: UUID,
        requested_task_action: Literal["continue", "new"],
        effective_task_action: Literal["continue", "new"],
        state_scope: StateScope,
        state_schema_version: str,
        outcome: Literal["completed", "rejected"],
        created_at: datetime,
    ) -> RuntimeEvent:
        """以 completed 或 rejected 正常关闭唯一 open Invocation。"""

        return await self._finish(
            event_type="internal.capability.invocation.completed",
            payload=InvocationCompletedPayload(
                invocation_id=invocation_id,
                capability_id=capability_id,
                task_id=task_id,
                requested_task_action=requested_task_action,
                effective_task_action=effective_task_action,
                state_scope=state_scope,
                state_schema_version=state_schema_version,
                outcome=outcome,
            ),
            created_at=created_at,
        )

    async def fail(
        self,
        *,
        invocation_id: UUID,
        capability_id: str,
        task_id: UUID | None,
        requested_task_action: Literal["continue", "new"],
        effective_task_action: Literal["continue", "new"] | None,
        state_scope: StateScope,
        state_schema_version: str | None,
        error_code: str,
        created_at: datetime,
    ) -> RuntimeEvent:
        """以稳定内部错误码关闭唯一 open Invocation。"""

        return await self._finish(
            event_type="internal.capability.invocation.failed",
            payload=InvocationFailedPayload(
                invocation_id=invocation_id,
                capability_id=capability_id,
                task_id=task_id,
                requested_task_action=requested_task_action,
                effective_task_action=effective_task_action,
                state_scope=state_scope,
                state_schema_version=state_schema_version,
                error_code=error_code,
            ),
            created_at=created_at,
        )

    async def cancel(
        self,
        *,
        invocation_id: UUID,
        capability_id: str,
        task_id: UUID | None,
        requested_task_action: Literal["continue", "new"],
        effective_task_action: Literal["continue", "new"] | None,
        state_scope: StateScope,
        state_schema_version: str | None,
        created_at: datetime,
    ) -> RuntimeEvent:
        """以显式取消语义关闭唯一 open Invocation。"""

        return await self._finish(
            event_type="internal.capability.invocation.cancelled",
            payload=InvocationCancelledPayload(
                invocation_id=invocation_id,
                capability_id=capability_id,
                task_id=task_id,
                requested_task_action=requested_task_action,
                effective_task_action=effective_task_action,
                state_scope=state_scope,
                state_schema_version=state_schema_version,
                outcome="cancelled",
            ),
            created_at=created_at,
        )

    async def _finish(
        self,
        *,
        event_type: str,
        payload: InvocationCompletedPayload
        | InvocationFailedPayload
        | InvocationCancelledPayload,
        created_at: datetime,
    ) -> RuntimeEvent:
        """统一提交不进入 Redis/SSE 的 Invocation 终态事件。"""

        event = await self._sequencer.finish_capability_invocation(
            RuntimeEventDraft(
                event_type=event_type,
                source="runtime.capability",
                visibility="internal",
                payload=payload,
                schema_version=1,
                durability="durable",
                created_at=created_at,
            )
        )
        log_business_event(
            logger,
            "Capability Invocation结束",
            run_id=event.run_id,
            invocation_id=event.payload["invocation_id"],
            capability_id=event.payload["capability_id"],
            event_type=event.event_type,
            requested_task_action=event.payload["requested_task_action"],
            effective_task_action=event.payload["effective_task_action"],
            state_scope=event.payload["state_scope"],
            state_schema_version=event.payload["state_schema_version"],
            error_code=event.payload.get("error_code"),
        )
        return event
