"""单 Run RuntimeEvent 序号分配与持久提交入口。"""

import asyncio
import logging
from datetime import datetime
from threading import Lock
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, TypeVar
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from agent_runtime.core.logging import log_business_event
from agent_runtime.runtime.event_models import (
    CapabilityInvocationEventCommit,
    RuntimeEvent,
    RuntimeEventDraft,
    RunEventCommit,
    SequenceBlock,
)
from agent_runtime.runtime.event_repository import (
    PostgresRuntimeEventRepository,
)
from agent_runtime.runtime.event_schemas import (
    RuntimeEventSchemaError,
    serialize_event_draft_payload,
)
from agent_runtime.runtime.models import JsonValue, RunStatus
from agent_runtime.runtime.interrupts import InterruptRunCommit

_SEQUENCER_CREATION_TOKEN = object()
logger = logging.getLogger(__name__)
_SEQUENCER_REGISTRY_LOCK = Lock()
_LIVE_SEQUENCERS: WeakValueDictionary[UUID, "RunSequencer"] = (
    WeakValueDictionary()
)
_CommitResult = TypeVar("_CommitResult")


class RuntimeEventPublisher(Protocol):
    """Run Sequencer 所需的最小实时事件发布接口。"""

    async def publish(self, event: RuntimeEvent) -> bool:
        """尽力发布一个已分配序号的 RuntimeEvent。"""

        ...


class RunSequencer:
    """在单进程内串行化一个 Run 的事件，并使用数据库序号块防止重号。"""

    @classmethod
    def for_run(
        cls,
        *,
        run_id: UUID,
        response_message_id: UUID,
        repository: PostgresRuntimeEventRepository,
        publisher: RuntimeEventPublisher | None = None,
        block_size: int = 32,
    ) -> "RunSequencer":
        """返回进程内该 Run 唯一的存活 Sequencer 实例。"""

        with _SEQUENCER_REGISTRY_LOCK:
            existing = _LIVE_SEQUENCERS.get(run_id)
            if existing is not None:
                if existing._response_message_id != response_message_id:
                    raise ValueError("同一 Run 的响应消息标识不得变化")
                if existing._block_size != block_size:
                    raise ValueError("同一 Run 的 Sequencer 序号块大小不得变化")
                if existing._publisher is not publisher:
                    raise ValueError("同一 Run 的 Sequencer 实时发布器不得变化")
                return existing
            sequencer = cls(
                run_id=run_id,
                response_message_id=response_message_id,
                repository=repository,
                publisher=publisher,
                block_size=block_size,
                _creation_token=_SEQUENCER_CREATION_TOKEN,
            )
            _LIVE_SEQUENCERS[run_id] = sequencer
            return sequencer

    def __init__(
        self,
        *,
        run_id: UUID,
        response_message_id: UUID,
        repository: PostgresRuntimeEventRepository,
        publisher: RuntimeEventPublisher | None = None,
        block_size: int = 32,
        _creation_token: object | None = None,
    ) -> None:
        if _creation_token is not _SEQUENCER_CREATION_TOKEN:
            raise TypeError("请使用 RunSequencer.for_run 获取单 Run 唯一实例")
        if block_size < 1:
            raise ValueError("RuntimeEvent 序号块大小必须大于等于 1")
        self._run_id = run_id
        self._response_message_id = response_message_id
        self._repository = repository
        self._publisher = publisher
        self._block_size = block_size
        self._block: SequenceBlock | None = None
        self._next_sequence: int | None = None
        self._active_attempt: int | None = None
        self._lock = asyncio.Lock()

    async def emit(self, draft: RuntimeEventDraft) -> RuntimeEvent:
        """校验并分配事件序号；只把 durable 事件写入 PostgreSQL。"""

        async with self._lock:
            event = await self._build_event(draft)
            if event.durability == "durable":
                stored = await self._repository.append_durable(event)
                if stored.event_type == "message.started":
                    self._active_attempt = int(stored.payload["attempt"])
                await self._publish_best_effort(stored)
                return stored
            await self._publish_best_effort(event)
            return event

    async def begin_capability_invocation(
        self,
        draft: RuntimeEventDraft,
    ) -> CapabilityInvocationEventCommit:
        """分配 started 序号，并由数据库原子新建或复用 open Invocation。"""

        async with self._lock:
            event = await self._build_event(draft)
            return await self._repository.begin_capability_invocation(event)

    async def finish_capability_invocation(
        self,
        draft: RuntimeEventDraft,
    ) -> RuntimeEvent:
        """分配终态序号，并由数据库与唯一 open Invocation 严格配对。"""

        async with self._lock:
            event = await self._build_event(draft)
            return await self._repository.finish_capability_invocation(event)

    async def get_open_capability_invocation(self) -> RuntimeEvent | None:
        """读取数据库中的唯一 open Invocation，供恢复诊断使用。"""

        return await self._repository.get_open_capability_invocation(
            run_id=self._run_id
        )

    async def commit_durable_internal_batch(
        self,
        *,
        drafts: Sequence[RuntimeEventDraft],
        commit: Callable[
            [tuple[RuntimeEvent, ...]],
            Awaitable[_CommitResult],
        ],
    ) -> _CommitResult:
        """在单 Run 锁内预分配内部 durable 事件，并交给业务事务原子写入。"""

        async with self._lock:
            events: list[RuntimeEvent] = []
            for draft in drafts:
                if draft.visibility != "internal" or draft.durability != "durable":
                    raise RuntimeEventSchemaError(
                        code="RUNTIME_EVENT_SCHEMA_INVALID",
                        message="事务批次只接受内部 durable RuntimeEvent",
                        status_code=409,
                    )
                events.append(await self._build_event(draft))
            return await commit(tuple(events))

    async def transition_run(
        self,
        *,
        target_status: RunStatus,
        event: RuntimeEventDraft,
        updated_at: datetime,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> RunEventCommit:
        """分配序号并原子提交 Run 状态与匹配的 durable 事件。"""

        async with self._lock:
            runtime_event = await self._build_event(event)
            commit = await self._repository.transition_run(
                run_id=self._run_id,
                target_status=target_status,
                event=runtime_event,
                updated_at=updated_at,
                error_code=error_code,
                error_message=error_message,
            )
            if commit.event is not None:
                await self._publish_best_effort(commit.event)
            return commit

    async def claim_recovery(
        self,
        *,
        event: RuntimeEventDraft,
        updated_at: datetime,
    ) -> RunEventCommit:
        """分配序号并原子递增恢复次数、接管 Run 与写入内部事件。"""

        async with self._lock:
            runtime_event = await self._build_event(event)
            commit = await self._repository.claim_recovery(
                run_id=self._run_id,
                event=runtime_event,
                updated_at=updated_at,
            )
            return commit

    async def require_interrupt(
        self,
        *,
        interrupt_id: UUID,
        interrupt_payload: dict[str, JsonValue],
        event: RuntimeEventDraft,
        updated_at: datetime,
    ) -> InterruptRunCommit:
        """分配序号并原子提交 pending Interrupt、Run 状态和公开事件。"""

        async with self._lock:
            runtime_event = await self._build_event(event)
            commit = await self._repository.require_interrupt(
                run_id=self._run_id,
                interrupt_id=interrupt_id,
                interrupt_payload=interrupt_payload,
                event=runtime_event,
                updated_at=updated_at,
            )
            if commit.event is not None:
                await self._publish_best_effort(commit.event)
            return commit

    async def resume_interrupt(
        self,
        *,
        interrupt_id: UUID,
        request_id: UUID,
        request_fingerprint: str,
        resume_payload: dict[str, JsonValue],
        event: RuntimeEventDraft,
        updated_at: datetime,
    ) -> InterruptRunCommit:
        """分配序号并行锁幂等恢复同一 Run，不重复发布已提交事件。"""

        async with self._lock:
            runtime_event = await self._build_event(event)
            commit = await self._repository.resume_interrupt(
                run_id=self._run_id,
                interrupt_id=interrupt_id,
                request_id=request_id,
                request_fingerprint=request_fingerprint,
                resume_payload=resume_payload,
                event=runtime_event,
                updated_at=updated_at,
            )
            if commit.event is not None:
                await self._publish_best_effort(commit.event)
            return commit

    async def _publish_best_effort(self, event: RuntimeEvent) -> None:
        """在数据库提交之后尽力发布，发布异常不得反向破坏 Run。"""

        if self._publisher is None or event.visibility != "public":
            return
        try:
            await self._publisher.publish(event)
        except Exception as error:
            if event.event_type != "message.delta":
                log_business_event(
                    logger,
                    "RuntimeEvent实时发布边界降级",
                    level=logging.WARNING,
                    run_id=event.run_id,
                    event_id=event.event_id,
                    event_type=event.event_type,
                    seq=event.seq,
                    error_code="RUNTIME_EVENT_PUBLISH_FAILED",
                    error_type=type(error).__name__,
                )
            return

    async def _build_event(self, draft: RuntimeEventDraft) -> RuntimeEvent:
        payload = serialize_event_draft_payload(draft)
        self._validate_message_event(draft.event_type, payload)
        seq = await self._allocate_sequence()
        return RuntimeEvent(
            event_id=uuid4(),
            run_id=self._run_id,
            seq=seq,
            event_type=draft.event_type,
            source=draft.source,
            visibility=draft.visibility,
            payload=payload,
            schema_version=draft.schema_version,
            durability=draft.durability,
            created_at=draft.created_at,
        )

    def _validate_message_event(
        self,
        event_type: str,
        payload: dict[str, JsonValue],
    ) -> None:
        """拒绝跨消息事件，并保证 delta 只属于当前生成尝试。"""

        if not event_type.startswith("message."):
            return
        if str(payload["response_message_id"]) != str(
            self._response_message_id
        ):
            raise RuntimeEventSchemaError(
                code="RUNTIME_EVENT_SCHEMA_INVALID",
                message="RuntimeEvent 响应消息标识与 Run 不一致",
                status_code=409,
            )
        if event_type != "message.delta":
            return
        if self._active_attempt is None or int(payload["attempt"]) != (
            self._active_attempt
        ):
            raise RuntimeEventSchemaError(
                code="RUNTIME_EVENT_SCHEMA_INVALID",
                message="message.delta 必须属于当前已开始的生成尝试",
                status_code=409,
            )

    async def _allocate_sequence(self) -> int:
        if (
            self._block is None
            or self._next_sequence is None
            or self._next_sequence > self._block.last
        ):
            self._block = await self._repository.reserve_sequence_block(
                run_id=self._run_id,
                block_size=self._block_size,
            )
            self._next_sequence = self._block.first
        allocated = self._next_sequence
        self._next_sequence += 1
        return allocated
