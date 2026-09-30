"""聊天应用层，连接 Session 产品能力、Active Run 与 Parent Graph。"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal, cast
from uuid import UUID, uuid4, uuid5

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, StateSnapshot

from agent_runtime.api.schemas.chat import (
    CapabilityId,
    DoneEventData,
    ErrorEventData,
    MessageEventData,
)
from agent_runtime.capabilities.en_to_zh.adapter import EnglishToChineseAdapter
from agent_runtime.capabilities.en_to_zh.graph import EnglishToChineseCapability
from agent_runtime.capabilities.general_chat.adapter import GeneralChatAdapter
from agent_runtime.capabilities.general_chat.agent import GeneralChatCapability
from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event
from agent_runtime.core.model import build_chat_model
from agent_runtime.feedback import (
    FeedbackAction,
    FeedbackResult,
    FeedbackService,
    PostgresFeedbackStore,
)
from agent_runtime.graph.config import (
    parent_thread_config,
    run_id_from_parent_config,
)
from agent_runtime.graph.parent import build_parent_graph
from agent_runtime.graph.router import StageOneRouter
from agent_runtime.history import MessageHistoryAdapter, MessagePage
from agent_runtime.persistence.checkpointers import open_stage_one_checkpointers
from agent_runtime.persistence.parent_state import PostgresParentStateStore
from agent_runtime.regeneration import CheckpointForker
from agent_runtime.runs import (
    ActiveRun,
    ActiveRunRegistry,
    CancelReason,
    ProductRunEvent,
)
from agent_runtime.runtime.event_repository import (
    PostgresRuntimeEventRepository,
)
from agent_runtime.runtime.event_gateway import GatewayItem, RuntimeEventGateway
from agent_runtime.runtime.event_models import RuntimeEventDraft
from agent_runtime.runtime.event_schemas import (
    InterruptRequiredPayload,
    InterruptResumedPayload,
    MessageDeltaPayload,
    MessageFinalizedPayload,
    MessageStartedPayload,
    RunCancelRequestedPayload,
    RunCancelledPayload,
    RunCompletedPayload,
    RunFailedPayload,
    RunRecoveryActivatedPayload,
    RunRecoveryClaimedPayload,
    RunStartedPayload,
)
from agent_runtime.runtime.checkpoint_recovery import (
    RecoveredFinalMessage,
    RunCheckpointInspector,
    ensure_automatic_recovery_safe,
    recovered_final_message,
)
from agent_runtime.runtime.fingerprints import build_request_fingerprint
from agent_runtime.runtime.models import JsonValue, Run, RunSubmission
from agent_runtime.runtime.interrupts import (
    InterruptStateConflictError,
    PostgresRunInterruptRepository,
    RunInterrupt,
    build_resume_request_fingerprint,
)
from agent_runtime.runtime.redis_stream import (
    RedisStreamPublisher,
    RedisStreamReader,
)
from agent_runtime.runtime.repository import (
    PostgresRunRepository,
    RunNotFoundError,
    RunRequestConflictError,
)
from agent_runtime.runtime.demo import build_runtime_demo_graph
from agent_runtime.runtime.sequencer import RunSequencer
from agent_runtime.runtime.coordinator import RunCoordinator
from agent_runtime.session_deletion import (
    SessionDeletionError,
    SessionDeletionService,
)
from agent_runtime.sessions.models import Session, SessionPage
from agent_runtime.sessions.repository import (
    PostgresSessionRepository,
    SessionNotFoundError,
)
from agent_runtime.sessions.service import SessionService

type CompletionStatus = Literal["completed", "unsupported"]

_FAILED_MESSAGE_FALLBACK = "抱歉，本次回复未能完成。"
_STOPPED_MESSAGE_FALLBACK = "已停止本次回复。"

logger = logging.getLogger(__name__)


class ChatSessionError(ApplicationError):
    """Session 所属关系或 Parent 持久化状态不合法。"""


class ChatRuntimeError(ApplicationError):
    """Parent Graph 流式事件或完成状态不符合 Stage 1 契约。"""


@dataclass(frozen=True, slots=True)
class PreparedChatTurn:
    """在建立 SSE 前完成校验并分配稳定标识的一轮聊天。"""

    session_id: UUID
    human_message: HumanMessage | None
    response_message_id: UUID
    config: RunnableConfig
    active_run: ActiveRun
    is_regeneration: bool = False
    is_resume: bool = False
    is_recovery: bool = False
    resume_payload: dict[str, JsonValue] | None = None


class ChatService:
    """查询 Session、准备聊天请求并维护 Parent Graph 公共消息。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_repository: PostgresSessionRepository,
        session_service: SessionService,
        parent_graph: Any,
        history_adapter: MessageHistoryAdapter | None = None,
        feedback_service: FeedbackService | None = None,
        deletion_service: SessionDeletionService | None = None,
        checkpoint_forker: CheckpointForker | None = None,
        run_registry: ActiveRunRegistry | None = None,
        persistent_run_repository: PostgresRunRepository | None = None,
        runtime_event_repository: PostgresRuntimeEventRepository | None = None,
        runtime_event_publisher: RedisStreamPublisher | None = None,
        runtime_event_gateway: RuntimeEventGateway | None = None,
        run_interrupt_repository: PostgresRunInterruptRepository | None = None,
        run_coordinator: RunCoordinator | None = None,
        session_id_factory: Callable[[], UUID] = uuid4,
        human_message_id_factory: Callable[[], UUID] = uuid4,
        response_message_id_factory: Callable[[], UUID] = uuid4,
        run_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        """绑定 Session 持久化、Parent Graph 和服务端 UUID 生成器。"""

        self._settings = settings or get_settings()
        self._session_repository = session_repository
        self._session_service = session_service
        self._parent_graph = parent_graph
        self._history_adapter = history_adapter or MessageHistoryAdapter(
            settings=self._settings,
            session_repository=session_repository,
        )
        self._feedback_service = feedback_service
        self._deletion_service = deletion_service
        self._checkpoint_forker = checkpoint_forker or CheckpointForker(
            history_adapter=self._history_adapter,
            parent_graph=parent_graph,
        )
        self._run_registry = run_registry or ActiveRunRegistry()
        self._persistent_run_repository = persistent_run_repository
        self._runtime_event_repository = runtime_event_repository
        self._runtime_event_publisher = runtime_event_publisher
        self._runtime_event_gateway = runtime_event_gateway
        self._run_interrupt_repository = run_interrupt_repository
        self._run_coordinator = run_coordinator
        self._session_id_factory = session_id_factory
        self._human_message_id_factory = human_message_id_factory
        self._response_message_id_factory = response_message_id_factory
        self._run_id_factory = run_id_factory
        self._run_submission_lock = asyncio.Lock()
        self._prepared_turns: dict[UUID, PreparedChatTurn] = {}
        self._turn_preparation_barriers: dict[
            UUID,
            tuple[ActiveRun, asyncio.Future[None]],
        ] = {}

    async def submit_chat_run(
        self,
        *,
        request_id: UUID,
        session_id: UUID | None,
        content: str,
    ) -> Run:
        """持久化普通消息 Run，事务提交后再唤醒本地执行器。"""

        if session_id is None:
            return await self._submit_chat_run_unlocked(
                request_id=request_id,
                session_id=None,
                content=content,
            )
        async with self._run_registry.session_operation(session_id):
            return await self._submit_chat_run_unlocked(
                request_id=request_id,
                session_id=session_id,
                content=content,
            )

    async def _submit_chat_run_unlocked(
        self,
        *,
        request_id: UUID,
        session_id: UUID | None,
        content: str,
    ) -> Run:
        """在 Session 删除屏障内执行普通持久 Run 的实际提交。"""

        repository, coordinator = self._persistent_runtime_dependencies()
        request_payload: dict[str, JsonValue] = {
            "message": {"content": content}
        }
        fingerprint = build_request_fingerprint(
            run_type="normal",
            session_id=session_id,
            request_payload=request_payload,
        )
        started_at = perf_counter()
        log_business_event(
            logger,
            "普通Run提交开始",
            request_id=request_id,
            session_id=session_id,
            session_mode="existing" if session_id is not None else "new",
        )
        async with self._run_submission_lock:
            existing = await self._resolve_existing_request(
                repository=repository,
                coordinator=coordinator,
                request_id=request_id,
                fingerprint=fingerprint,
            )
            if existing is not None:
                return existing

            created_at = datetime.now(UTC)
            response_message_id = self._response_message_id_factory()
            input_message_id = self._human_message_id_factory()
            new_session: Session | None = None
            start_checkpoint_id: str | None = None
            if session_id is None:
                resolved_session_id = self._session_id_factory()
                new_session = Session(
                    session_id=resolved_session_id,
                    user_id=self._settings.local_user_id,
                    title=content,
                    created_at=created_at,
                    updated_at=created_at,
                )
            else:
                resolved_session_id = session_id
                state = await self._get_owned_parent_state(session_id)
                start_checkpoint_id = self._checkpoint_id(state.config)

            result = await repository.create_or_get(
                RunSubmission(
                    run_id=self._run_id_factory(),
                    request_id=request_id,
                    session_id=resolved_session_id,
                    thread_id=str(resolved_session_id),
                    parent_run_id=None,
                    run_type="normal",
                    input_message_id=input_message_id,
                    response_message_id=response_message_id,
                    start_checkpoint_id=start_checkpoint_id,
                    input_payload=request_payload,
                    request_fingerprint=fingerprint,
                    created_at=created_at,
                ),
                new_session=new_session,
            )
        await coordinator.wake(result.run.run_id)
        log_business_event(
            logger,
            "普通Run提交完成",
            run_id=result.run.run_id,
            request_id=result.run.request_id,
            session_id=result.run.session_id,
            message_id=result.run.response_message_id,
            status=result.run.status,
            created=result.created,
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        )
        return result.run

    async def submit_regeneration_run(
        self,
        *,
        request_id: UUID,
        session_id: UUID,
        message_id: UUID,
    ) -> Run:
        """校验并持久化 Regenerate Run，不新增 HumanMessage。"""

        async with self._run_registry.session_operation(session_id):
            return await self._submit_regeneration_run_unlocked(
                request_id=request_id,
                session_id=session_id,
                message_id=message_id,
            )

    async def _submit_regeneration_run_unlocked(
        self,
        *,
        request_id: UUID,
        session_id: UUID,
        message_id: UUID,
    ) -> Run:
        """在 Session 删除屏障内执行 Regenerate Run 的实际提交。"""

        repository, coordinator = self._persistent_runtime_dependencies()
        request_payload: dict[str, JsonValue] = {
            "source_message_id": str(message_id)
        }
        fingerprint = build_request_fingerprint(
            run_type="regenerate",
            session_id=session_id,
            request_payload=request_payload,
        )
        started_at = perf_counter()
        log_business_event(
            logger,
            "重新生成Run提交开始",
            request_id=request_id,
            session_id=session_id,
            source_message_id=message_id,
        )
        async with self._run_submission_lock:
            existing = await self._resolve_existing_request(
                repository=repository,
                coordinator=coordinator,
                request_id=request_id,
                fingerprint=fingerprint,
            )
            if existing is not None:
                return existing

            await self._require_owned_session(session_id)
            messages = await self._history_adapter.get_active_messages(
                session_id=session_id
            )
            if (
                not messages
                or messages[-1].message_id != message_id
                or messages[-1].role != "assistant"
                or messages[-1].runtime_status != "completed"
            ):
                raise ChatRuntimeError(
                    code="MESSAGE_REGENERATE_NOT_ALLOWED",
                    message="仅允许重新生成当前活动分支最新的 completed AIMessage",
                    status_code=409,
                )
            source_human = next(
                (
                    message
                    for message in reversed(messages[:-1])
                    if message.role == "user"
                ),
                None,
            )
            if source_human is None:
                raise ChatRuntimeError(
                    code="MESSAGE_REGENERATE_NOT_ALLOWED",
                    message="重新生成的原回答缺少对应用户消息",
                    status_code=409,
                )
            start_config = await self._checkpoint_forker.find_start_checkpoint(
                session_id=session_id,
                message_id=message_id,
            )
            start_checkpoint_id = self._checkpoint_id(start_config)
            if start_checkpoint_id is None:
                raise ChatRuntimeError(
                    code="MESSAGE_REGENERATE_FORK_FAILED",
                    message="创建消息重新生成分支失败",
                    retryable=True,
                )
            source_run = await repository.find_by_response_message_id(
                session_id=session_id,
                response_message_id=message_id,
            )
            response_message_id = self._response_message_id_factory()
            created_at = datetime.now(UTC)
            result = await repository.create_or_get(
                RunSubmission(
                    run_id=self._run_id_factory(),
                    request_id=request_id,
                    session_id=session_id,
                    thread_id=str(session_id),
                    parent_run_id=(
                        source_run.run_id if source_run is not None else None
                    ),
                    run_type="regenerate",
                    input_message_id=source_human.message_id,
                    response_message_id=response_message_id,
                    start_checkpoint_id=start_checkpoint_id,
                    input_payload=request_payload,
                    request_fingerprint=fingerprint,
                    created_at=created_at,
                )
            )
        await coordinator.wake(result.run.run_id)
        log_business_event(
            logger,
            "重新生成Run提交完成",
            run_id=result.run.run_id,
            request_id=result.run.request_id,
            session_id=result.run.session_id,
            source_message_id=message_id,
            message_id=result.run.response_message_id,
            status=result.run.status,
            created=result.created,
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        )
        return result.run

    async def get_active_run(self, *, session_id: UUID) -> Run | None:
        """验证固定用户所有权后返回 Session 当前活动 Run。"""

        repository, _coordinator = self._persistent_runtime_dependencies()
        await self._require_owned_session(session_id)
        return await repository.get_active_for_session(session_id)

    async def get_pending_interrupt(
        self,
        *,
        run_id: UUID,
    ) -> RunInterrupt:
        """验证 Run 所有权并返回刷新页面所需的唯一待处理中断。"""

        repository, _coordinator = self._persistent_runtime_dependencies()
        interrupt_repository = self._require_interrupt_repository()
        run = await repository.get(run_id)
        await self._require_owned_session(run.session_id)
        pending = await interrupt_repository.get_pending(run_id=run_id)
        if pending is None:
            raise InterruptStateConflictError(
                code="INTERRUPT_STATE_CONFLICT",
                message="Run 当前没有待处理 Interrupt",
                status_code=409,
            )
        return pending

    async def resume_persistent_run(
        self,
        *,
        run_id: UUID,
        interrupt_id: UUID,
        request_id: UUID,
        resume_payload: dict[str, JsonValue],
    ) -> Run:
        """原子消费 Interrupt，沿用同一 Run 提交恢复并唤醒本地执行器。"""

        repository, coordinator = self._persistent_runtime_dependencies()
        event_repository = self._runtime_event_repository
        if event_repository is None:
            raise ChatRuntimeError(
                code="RUNTIME_EVENT_SERVICE_UNAVAILABLE",
                message="RuntimeEvent 服务尚未完成初始化",
                retryable=True,
            )
        run = await repository.get(run_id)
        await self._require_owned_session(run.session_id)
        fingerprint = build_resume_request_fingerprint(
            run_id=run_id,
            interrupt_id=interrupt_id,
            resume_payload=resume_payload,
        )
        started_at = perf_counter()
        log_business_event(
            logger,
            "持久Run恢复开始",
            run_id=run_id,
            request_id=request_id,
            session_id=run.session_id,
            interrupt_id=interrupt_id,
            status=run.status,
        )
        sequencer = RunSequencer.for_run(
            run_id=run.run_id,
            response_message_id=run.response_message_id,
            repository=event_repository,
            publisher=self._runtime_event_publisher,
        )
        async with coordinator.claim_guard(run_id):
            commit = await sequencer.resume_interrupt(
                interrupt_id=interrupt_id,
                request_id=request_id,
                request_fingerprint=fingerprint,
                resume_payload=resume_payload,
                event=self._runtime_event_draft(
                    "interrupt.resumed",
                    InterruptResumedPayload(interrupt_id=interrupt_id),
                ),
                updated_at=datetime.now(UTC),
            )
        # 幂等重试也重复发送进程内唤醒；Coordinator 会按 run_id 去重，
        # 从而补偿“事务已提交但首次唤醒未完成”的窗口。
        await coordinator.wake_resumed(run_id)
        log_business_event(
            logger,
            "持久Run恢复已接受",
            run_id=run_id,
            request_id=request_id,
            session_id=commit.run.session_id,
            interrupt_id=interrupt_id,
            status=commit.run.status,
            idempotent=not commit.changed,
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        )
        return commit.run

    async def open_run_event_stream(
        self,
        *,
        run_id: UUID,
        after_seq: int,
    ) -> AsyncIterator[GatewayItem]:
        """在建立 SSE 前校验 Run 所属 Session，并返回独立 Gateway 流。"""

        repository = self._persistent_run_repository
        gateway = self._runtime_event_gateway
        if repository is None or gateway is None:
            raise ChatRuntimeError(
                code="RUN_EVENT_SERVICE_UNAVAILABLE",
                message="Run 事件服务尚未完成初始化",
                retryable=True,
            )
        run = await repository.get(run_id)
        await self._require_owned_session(run.session_id)
        log_business_event(
            logger,
            "Run事件流访问通过",
            run_id=run.run_id,
            request_id=run.request_id,
            session_id=run.session_id,
            message_id=run.response_message_id,
            status=run.status,
            after_seq=after_seq,
        )
        return gateway.stream(run_id=run_id, after_seq=after_seq)

    async def cancel_persistent_run(self, *, run_id: UUID) -> Run:
        """幂等接受显式取消，并立即返回 PostgreSQL 中的当前权威状态。"""

        repository, coordinator = self._persistent_runtime_dependencies()
        run = await repository.get(run_id)
        await self._require_owned_session(run.session_id)
        log_business_event(
            logger,
            "持久Run取消开始",
            run_id=run.run_id,
            request_id=run.request_id,
            session_id=run.session_id,
            message_id=run.response_message_id,
            status=run.status,
        )
        if run.status in {"completed", "failed", "cancelled"}:
            log_business_event(
                logger,
                "持久Run取消幂等返回",
                run_id=run.run_id,
                request_id=run.request_id,
                session_id=run.session_id,
                message_id=run.response_message_id,
                status=run.status,
            )
            return run

        async with coordinator.claim_guard(run_id):
            run = await repository.get(run_id)
            if run.status in {"queued", "interrupted"}:
                run = await self._finalize_persistent_cancellation_shielded(run)
            elif run.status in {"running", "recovering"}:
                event_repository = self._runtime_event_repository
                if event_repository is None:
                    raise ChatRuntimeError(
                        code="RUNTIME_EVENT_SERVICE_UNAVAILABLE",
                        message="RuntimeEvent 服务尚未完成初始化",
                        retryable=True,
                    )
                sequencer = RunSequencer.for_run(
                    run_id=run.run_id,
                    response_message_id=run.response_message_id,
                    repository=event_repository,
                    publisher=self._runtime_event_publisher,
                )
                commit = await sequencer.transition_run(
                    target_status="cancel_requested",
                    event=self._internal_runtime_event_draft(
                        "internal.run.cancel_requested",
                        RunCancelRequestedPayload(status="cancel_requested"),
                    ),
                    updated_at=datetime.now(UTC),
                )
                run = commit.run
            if run.status == "cancel_requested":
                await coordinator.request_cancel(run.run_id)
        if run.status == "cancel_requested":
            await coordinator.wake(run.run_id)

        log_business_event(
            logger,
            "持久Run取消已接受",
            run_id=run.run_id,
            request_id=run.request_id,
            session_id=run.session_id,
            message_id=run.response_message_id,
            status=run.status,
        )
        return run

    async def _finalize_persistent_cancellation_shielded(
        self,
        run: Run,
    ) -> Run:
        """延迟传播调用方取消，确保直接取消终态收尾先完整结束。"""

        finalization = asyncio.create_task(
            self._finalize_persistent_cancellation_locked(run),
            name=f"run-direct-cancel:{run.run_id}",
        )
        try:
            return await asyncio.shield(finalization)
        except asyncio.CancelledError:
            # shield 已隔离首次调用方取消；继续持有决胜锁直到收尾完成，
            # 再把取消传播给 HTTP 或 Session 删除调用方。
            await finalization
            raise

    async def execute_persistent_run(
        self,
        run: Run,
        dispatch_reason: str = "submit",
        *,
        recovery_config: RunnableConfig | None = None,
    ) -> None:
        """执行新 Run、现场 Resume 或按 Checkpoint 对账后的恢复尝试。"""

        repository, coordinator = self._persistent_runtime_dependencies()
        event_repository = self._runtime_event_repository
        if event_repository is None:
            raise ChatRuntimeError(
                code="RUNTIME_EVENT_SERVICE_UNAVAILABLE",
                message="RuntimeEvent 服务尚未完成初始化",
                retryable=True,
            )
        is_resume = dispatch_reason == "resume"
        is_recovery_execution = dispatch_reason == "recovery-active"
        if (
            not is_resume
            and not is_recovery_execution
            and run.status in {"queued", "interrupted", "cancel_requested"}
            and await self._reconcile_stopped_checkpoint(run)
        ):
            return
        if run.status == "cancel_requested":
            await coordinator.begin_cancel_finalization(run.run_id)
            async with coordinator.claim_guard(run.run_id):
                current = await repository.get(run.run_id)
                if current.status == "cancel_requested":
                    await self._finalize_persistent_cancellation_locked(current)
            return
        if (
            run.status in {"running", "recovering"}
            and not is_resume
            and not is_recovery_execution
        ):
            await self._recover_persistent_run(run)
            return
        resumed_interrupt = None
        if is_resume:
            resumed_interrupt = await self._require_interrupt_repository().get_latest_resumed(
                run_id=run.run_id
            )
            if (
                resumed_interrupt is None
                or resumed_interrupt.resume_payload is None
            ):
                return
        elif run.status != "queued" and not is_recovery_execution:
            return
        sequencer = RunSequencer.for_run(
            run_id=run.run_id,
            response_message_id=run.response_message_id,
            repository=event_repository,
            publisher=self._runtime_event_publisher,
        )
        started_at = perf_counter()
        capability_id: CapabilityId | None = None
        log_business_event(
            logger,
            "持久Run执行开始",
            run_id=run.run_id,
            request_id=run.request_id,
            session_id=run.session_id,
            message_id=run.response_message_id,
            run_type=run.run_type,
        )
        async with coordinator.claim_guard(run.run_id):
            current = await repository.get(run.run_id)
            if current.status == "cancel_requested":
                await coordinator.begin_cancel_finalization(run.run_id)
                await self._finalize_persistent_cancellation_locked(current)
                return
            if current.status in {"completed", "failed", "cancelled"}:
                return
            if is_resume or is_recovery_execution:
                if current.status != "running":
                    return
            else:
                if current.status != "queued":
                    return
                await sequencer.transition_run(
                    target_status="running",
                    event=self._runtime_event_draft(
                        "run.started",
                        RunStartedPayload(status="running"),
                    ),
                    updated_at=datetime.now(UTC),
                )
        partial_text: list[str] = []
        turn: PreparedChatTurn | None = None
        try:
            turn = await self._turn_from_run(
                run,
                is_resume=is_resume,
                resume_payload=(
                    resumed_interrupt.resume_payload
                    if resumed_interrupt is not None
                    else None
                ),
                recovery_config=recovery_config,
            )
            attempt = await event_repository.next_message_attempt(
                run_id=run.run_id
            )
            await sequencer.emit(
                self._runtime_event_draft(
                    "message.started",
                    MessageStartedPayload(
                        response_message_id=run.response_message_id,
                        attempt=attempt,
                    ),
                )
            )
            async for message in self.stream_turn(turn):
                if await coordinator.is_cancel_requested(run.run_id):
                    await coordinator.begin_cancel_finalization(run.run_id)
                    raise asyncio.CancelledError
                capability_id = self._message_capability_id(
                    message,
                    fallback=capability_id,
                )
                delta = str(message.text)
                if not delta:
                    continue
                partial_text.append(delta)
                await sequencer.emit(
                    self._runtime_event_draft(
                        "message.delta",
                        MessageDeltaPayload(
                            response_message_id=run.response_message_id,
                            attempt=attempt,
                            delta=delta,
                        ),
                    )
                )
            if await coordinator.is_cancel_requested(run.run_id):
                await coordinator.begin_cancel_finalization(run.run_id)
                raise asyncio.CancelledError
            graph_interrupt = await self._pending_graph_interrupt(
                run=run,
                turn=turn,
            )
            if graph_interrupt is not None:
                interrupt_id, interrupt_payload = graph_interrupt
                async with coordinator.claim_guard(run.run_id):
                    current = await repository.get(run.run_id)
                    if current.status == "cancel_requested":
                        await coordinator.begin_cancel_finalization(run.run_id)
                        await self._finalize_persistent_cancellation_locked(
                            current,
                            partial_content="".join(partial_text),
                            capability_id=capability_id,
                            turn=turn,
                        )
                        return
                    if current.status != "running":
                        return
                    await sequencer.require_interrupt(
                        interrupt_id=interrupt_id,
                        interrupt_payload=interrupt_payload,
                        event=self._runtime_event_draft(
                            "interrupt.required",
                            InterruptRequiredPayload(
                                interrupt_id=interrupt_id
                            ),
                        ),
                        updated_at=datetime.now(UTC),
                    )
                    await self._session_service.touch_session(
                        session_id=run.session_id
                    )
                log_business_event(
                    logger,
                    "持久Run等待人工输入",
                    run_id=run.run_id,
                    request_id=run.request_id,
                    session_id=run.session_id,
                    message_id=run.response_message_id,
                    interrupt_id=interrupt_id,
                    status="interrupted",
                    duration_ms=round(
                        (perf_counter() - started_at) * 1000,
                        2,
                    ),
                )
                return
            completion_status = await self.get_completion_status(turn)
            await self._require_nonempty_persistent_message(
                run=run,
                expected_status=completion_status,
            )
            async with coordinator.claim_guard(run.run_id):
                current = await repository.get(run.run_id)
                if current.status == "cancel_requested":
                    await coordinator.begin_cancel_finalization(run.run_id)
                    await self._finalize_persistent_cancellation_locked(
                        current,
                        partial_content="".join(partial_text),
                        capability_id=capability_id,
                        turn=turn,
                    )
                    return
                if current.status != "running":
                    return
                await self._session_service.touch_session(
                    session_id=run.session_id
                )
                await sequencer.emit(
                    self._runtime_event_draft(
                        "message.finalized",
                        MessageFinalizedPayload(
                            response_message_id=run.response_message_id,
                            runtime_status=completion_status,
                            capability_id=capability_id,
                        ),
                    )
                )
                await sequencer.transition_run(
                    target_status="completed",
                    event=self._runtime_event_draft(
                        "run.completed",
                        RunCompletedPayload(status="completed"),
                    ),
                    updated_at=datetime.now(UTC),
                )
        except asyncio.CancelledError:
            if not await coordinator.is_cancel_requested(run.run_id):
                raise
            await coordinator.begin_cancel_finalization(run.run_id)
            async with coordinator.claim_guard(run.run_id):
                current = await repository.get(run.run_id)
                if current.status == "cancel_requested":
                    await self._finalize_persistent_cancellation_locked(
                        current,
                        partial_content="".join(partial_text),
                        capability_id=capability_id,
                        turn=turn,
                    )
            return
        except Exception as error:
            public_error = (
                error
                if isinstance(error, ApplicationError)
                else ChatRuntimeError(
                    code="CHAT_RUNTIME_FAILED",
                    message="聊天运行失败",
                    retryable=False,
                )
            )
            async with coordinator.claim_guard(run.run_id):
                current = await repository.get(run.run_id)
                if current.status == "cancel_requested":
                    await coordinator.begin_cancel_finalization(run.run_id)
                    await self._finalize_persistent_cancellation_locked(
                        current,
                        partial_content="".join(partial_text),
                        capability_id=capability_id,
                        turn=turn,
                    )
                    return
                if current.status in {"completed", "failed", "cancelled"}:
                    return
                if turn is None:
                    # 无法构造稳定 Parent 写入位置时不得先提交数据库终态；
                    # 保留非终态供后续恢复对账重试。
                    turn = await self._turn_from_run(run)
                failure_content = "".join(partial_text)
                if not failure_content.strip():
                    failure_content = _FAILED_MESSAGE_FALLBACK
                await self.persist_runtime_message(
                    turn,
                    failure_content,
                    runtime_status="incomplete",
                    capability_id=capability_id,
                    include_human=(
                        not turn.is_regeneration
                        and not turn.is_resume
                        and turn.human_message is not None
                    ),
                )
                await sequencer.emit(
                    self._runtime_event_draft(
                        "message.finalized",
                        MessageFinalizedPayload(
                            response_message_id=run.response_message_id,
                            runtime_status="incomplete",
                            capability_id=capability_id,
                        ),
                    )
                )
                await sequencer.transition_run(
                    target_status="failed",
                    event=self._runtime_event_draft(
                        "run.failed",
                        RunFailedPayload(
                            status="failed",
                            code=public_error.code,
                            message=public_error.message,
                            retryable=public_error.retryable,
                        ),
                    ),
                    updated_at=datetime.now(UTC),
                    error_code=public_error.code,
                    error_message=public_error.message,
                )
            log_business_event(
                logger,
                "持久Run执行失败",
                level=logging.ERROR,
                run_id=run.run_id,
                request_id=run.request_id,
                session_id=run.session_id,
                message_id=run.response_message_id,
                capability_id=capability_id,
                error_code=public_error.code,
                error_type=type(error).__name__,
                duration_ms=round((perf_counter() - started_at) * 1000, 2),
            )
            return
        log_business_event(
            logger,
            "持久Run执行完成",
            run_id=run.run_id,
            request_id=run.request_id,
            session_id=run.session_id,
            message_id=run.response_message_id,
            capability_id=capability_id,
            status="completed",
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        )

    async def _reconcile_stopped_checkpoint(self, run: Run) -> bool:
        """识别已落盘 stopped 消息，并在执行 Graph 前只补取消投影。"""

        inspector = RunCheckpointInspector(parent_graph=self._parent_graph)
        snapshot = await inspector.latest_for_run(run)
        if snapshot is None:
            return False
        final_message = recovered_final_message(run, snapshot)
        if (
            final_message is None
            or final_message.runtime_status != "stopped"
        ):
            return False

        repository, coordinator = self._persistent_runtime_dependencies()
        event_repository = self._runtime_event_repository
        if event_repository is None:
            raise ChatRuntimeError(
                code="RUNTIME_EVENT_SERVICE_UNAVAILABLE",
                message="RuntimeEvent 服务尚未完成初始化",
                retryable=True,
            )
        sequencer = RunSequencer.for_run(
            run_id=run.run_id,
            response_message_id=run.response_message_id,
            repository=event_repository,
            publisher=self._runtime_event_publisher,
        )
        await coordinator.begin_cancel_finalization(run.run_id)
        async with coordinator.claim_guard(run.run_id):
            current = await repository.get(run.run_id)
            if current.status in {"completed", "failed", "cancelled"}:
                return True
            await self._project_stopped_cancellation_locked(
                current,
                final_message=final_message,
                sequencer=sequencer,
            )
        return True

    async def _project_stopped_cancellation_locked(
        self,
        run: Run,
        *,
        final_message: RecoveredFinalMessage,
        sequencer: RunSequencer,
    ) -> Run:
        """幂等补齐 stopped 消息对应的唯一取消事件和 Run 终态。"""

        event_repository = self._runtime_event_repository
        assert event_repository is not None
        current = run
        if current.status in {"completed", "failed", "cancelled"}:
            return current
        if current.status in {"running", "recovering"}:
            requested = await sequencer.transition_run(
                target_status="cancel_requested",
                event=self._internal_runtime_event_draft(
                    "internal.run.cancel_requested",
                    RunCancelRequestedPayload(status="cancel_requested"),
                ),
                updated_at=datetime.now(UTC),
            )
            current = requested.run
        if current.status not in {
            "queued",
            "interrupted",
            "cancel_requested",
        }:
            return current
        if not await event_repository.has_event_type(
            run_id=run.run_id,
            event_type="message.finalized",
        ):
            await sequencer.emit(
                self._runtime_event_draft(
                    "message.finalized",
                    MessageFinalizedPayload(
                        response_message_id=run.response_message_id,
                        runtime_status="stopped",
                        capability_id=final_message.capability_id,
                    ),
                )
            )
        commit = await sequencer.transition_run(
            target_status="cancelled",
            event=self._runtime_event_draft(
                "run.cancelled",
                RunCancelledPayload(status="cancelled"),
            ),
            updated_at=datetime.now(UTC),
        )
        await self._session_service.touch_session(session_id=run.session_id)
        log_business_event(
            logger,
            "持久Run停止Checkpoint取消投影完成",
            run_id=run.run_id,
            request_id=run.request_id,
            session_id=run.session_id,
            message_id=run.response_message_id,
            capability_id=final_message.capability_id,
            status="cancelled",
        )
        return commit.run

    async def _recover_persistent_run(self, run: Run) -> None:
        """接管遗留活动 Run，并优先从精确 Checkpoint 补齐持久投影。"""

        repository, coordinator = self._persistent_runtime_dependencies()
        event_repository = self._runtime_event_repository
        if event_repository is None:
            raise ChatRuntimeError(
                code="RUNTIME_EVENT_SERVICE_UNAVAILABLE",
                message="RuntimeEvent 服务尚未完成初始化",
                retryable=True,
            )
        inspector = RunCheckpointInspector(parent_graph=self._parent_graph)
        snapshot = await inspector.latest_for_run(run)
        final_message = (
            recovered_final_message(run, snapshot)
            if snapshot is not None
            else None
        )
        graph_interrupt = (
            self._graph_interrupt_from_snapshot(run=run, snapshot=snapshot)
            if snapshot is not None
            else None
        )
        sequencer = RunSequencer.for_run(
            run_id=run.run_id,
            response_message_id=run.response_message_id,
            repository=event_repository,
            publisher=self._runtime_event_publisher,
        )
        if (
            final_message is not None
            and final_message.runtime_status == "stopped"
        ):
            await coordinator.begin_cancel_finalization(run.run_id)
            async with coordinator.claim_guard(run.run_id):
                current = await repository.get(run.run_id)
                if current.status in {
                    "queued",
                    "running",
                    "recovering",
                    "interrupted",
                    "cancel_requested",
                }:
                    await self._project_stopped_cancellation_locked(
                        current,
                        final_message=final_message,
                        sequencer=sequencer,
                    )
            return
        claimed_run = run
        async with coordinator.claim_guard(run.run_id):
            current = await repository.get(run.run_id)
            if current.status == "cancel_requested":
                await coordinator.begin_cancel_finalization(run.run_id)
                await self._finalize_persistent_cancellation_locked(current)
                return
            if current.status not in {"running", "recovering"}:
                return
            if current.recovery_attempts >= 3:
                if final_message is None and graph_interrupt is None:
                    await self._fail_recovery_exhausted_locked(
                        current,
                        snapshot=snapshot,
                        sequencer=sequencer,
                    )
                    return
                claimed_run = current
            else:
                next_attempt = current.recovery_attempts + 1
                claim = await sequencer.claim_recovery(
                    event=self._internal_runtime_event_draft(
                        "internal.run.recovery_claimed",
                        RunRecoveryClaimedPayload(
                            status="recovering",
                            recovery_attempt=next_attempt,
                        ),
                    ),
                    updated_at=datetime.now(UTC),
                )
                claimed_run = claim.run

        if final_message is not None:
            await self._project_recovered_final_message(
                claimed_run,
                final_message=final_message,
                sequencer=sequencer,
            )
            return
        if graph_interrupt is not None:
            await self._project_recovered_interrupt(
                claimed_run,
                graph_interrupt=graph_interrupt,
                sequencer=sequencer,
            )
            return

        if snapshot is not None:
            ensure_automatic_recovery_safe(snapshot)
        async with coordinator.claim_guard(run.run_id):
            current = await repository.get(run.run_id)
            if current.status == "cancel_requested":
                await coordinator.begin_cancel_finalization(run.run_id)
                await self._finalize_persistent_cancellation_locked(current)
                return
            if current.status != "recovering":
                return
            activation = await sequencer.transition_run(
                target_status="running",
                event=self._internal_runtime_event_draft(
                    "internal.run.recovery_activated",
                    RunRecoveryActivatedPayload(
                        status="running",
                        recovery_attempt=current.recovery_attempts,
                    ),
                ),
                updated_at=datetime.now(UTC),
            )
            active_run = activation.run

        recovery_config = (
            inspector.recovery_config(active_run, snapshot)
            if snapshot is not None
            else None
        )
        log_business_event(
            logger,
            "持久Run恢复执行开始",
            run_id=active_run.run_id,
            request_id=active_run.request_id,
            session_id=active_run.session_id,
            message_id=active_run.response_message_id,
            recovery_attempt=active_run.recovery_attempts,
            checkpoint_found=snapshot is not None,
        )
        await self.execute_persistent_run(
            active_run,
            "recovery-active",
            recovery_config=recovery_config,
        )

    async def _project_recovered_final_message(
        self,
        run: Run,
        *,
        final_message: RecoveredFinalMessage,
        sequencer: RunSequencer,
    ) -> None:
        """Checkpoint 已有终态消息时只补事件和 Run 投影，不重跑 Graph。"""

        repository, coordinator = self._persistent_runtime_dependencies()
        event_repository = self._runtime_event_repository
        assert event_repository is not None
        if final_message.runtime_status == "stopped":
            await coordinator.begin_cancel_finalization(run.run_id)
        async with coordinator.claim_guard(run.run_id):
            current = await repository.get(run.run_id)
            if final_message.runtime_status == "stopped":
                await self._project_stopped_cancellation_locked(
                    current,
                    final_message=final_message,
                    sequencer=sequencer,
                )
                return
            if current.status == "cancel_requested":
                await coordinator.begin_cancel_finalization(run.run_id)
                await self._finalize_persistent_cancellation_locked(current)
                return
            if current.status not in {"running", "recovering"}:
                return
            if (
                current.status == "recovering"
                and final_message.runtime_status
                in {"completed", "unsupported"}
            ):
                activation = await sequencer.transition_run(
                    target_status="running",
                    event=self._internal_runtime_event_draft(
                        "internal.run.recovery_activated",
                        RunRecoveryActivatedPayload(
                            status="running",
                            recovery_attempt=current.recovery_attempts,
                        ),
                    ),
                    updated_at=datetime.now(UTC),
                )
                current = activation.run
            if not await event_repository.has_event_type(
                run_id=run.run_id,
                event_type="message.finalized",
            ):
                await sequencer.emit(
                    self._runtime_event_draft(
                        "message.finalized",
                        MessageFinalizedPayload(
                            response_message_id=run.response_message_id,
                            runtime_status=final_message.runtime_status,
                            capability_id=final_message.capability_id,
                        ),
                    )
                )
            if final_message.runtime_status in {"completed", "unsupported"}:
                await sequencer.transition_run(
                    target_status="completed",
                    event=self._runtime_event_draft(
                        "run.completed",
                        RunCompletedPayload(status="completed"),
                    ),
                    updated_at=datetime.now(UTC),
                )
            else:
                await sequencer.transition_run(
                    target_status="failed",
                    event=self._runtime_event_draft(
                        "run.failed",
                        RunFailedPayload(
                            status="failed",
                            code="RUN_RECOVERED_INCOMPLETE",
                            message="进程中断前的未完成回复已恢复。",
                            retryable=True,
                        ),
                    ),
                    updated_at=datetime.now(UTC),
                    error_code="RUN_RECOVERED_INCOMPLETE",
                    error_message="进程中断前的未完成回复已恢复。",
                )
            await self._session_service.touch_session(session_id=run.session_id)
        log_business_event(
            logger,
            "持久RunCheckpoint终态投影完成",
            run_id=run.run_id,
            session_id=run.session_id,
            message_id=run.response_message_id,
            status=final_message.runtime_status,
            recovery_attempt=run.recovery_attempts,
        )

    async def _project_recovered_interrupt(
        self,
        run: Run,
        *,
        graph_interrupt: tuple[UUID, dict[str, JsonValue]],
        sequencer: RunSequencer,
    ) -> None:
        """Checkpoint 已有中断时只补 Interrupt、Run 与事件的原子投影。"""

        repository, coordinator = self._persistent_runtime_dependencies()
        interrupt_id, interrupt_payload = graph_interrupt
        async with coordinator.claim_guard(run.run_id):
            current = await repository.get(run.run_id)
            if current.status == "cancel_requested":
                await coordinator.begin_cancel_finalization(run.run_id)
                await self._finalize_persistent_cancellation_locked(current)
                return
            if current.status not in {"running", "recovering"}:
                return
            await sequencer.require_interrupt(
                interrupt_id=interrupt_id,
                interrupt_payload=interrupt_payload,
                event=self._runtime_event_draft(
                    "interrupt.required",
                    InterruptRequiredPayload(interrupt_id=interrupt_id),
                ),
                updated_at=datetime.now(UTC),
            )
            await self._session_service.touch_session(session_id=run.session_id)
        log_business_event(
            logger,
            "持久RunCheckpoint中断投影完成",
            run_id=run.run_id,
            session_id=run.session_id,
            message_id=run.response_message_id,
            interrupt_id=interrupt_id,
            status="interrupted",
            recovery_attempt=run.recovery_attempts,
        )

    async def _fail_recovery_exhausted_locked(
        self,
        run: Run,
        *,
        snapshot: StateSnapshot | None,
        sequencer: RunSequencer,
    ) -> None:
        """三次恢复均未形成终态时保存非空回复并提交唯一失败终态。"""

        event_repository = self._runtime_event_repository
        assert event_repository is not None
        inspector = RunCheckpointInspector(parent_graph=self._parent_graph)
        turn = await self._turn_from_run(
            run,
            recovery_config=(
                inspector.recovery_config(run, snapshot)
                if snapshot is not None
                else None
            ),
        )
        await self.persist_runtime_message(
            turn,
            _FAILED_MESSAGE_FALLBACK,
            runtime_status="incomplete",
            capability_id=None,
            include_human=(turn.human_message is not None),
        )
        if not await event_repository.has_event_type(
            run_id=run.run_id,
            event_type="message.finalized",
        ):
            await sequencer.emit(
                self._runtime_event_draft(
                    "message.finalized",
                    MessageFinalizedPayload(
                        response_message_id=run.response_message_id,
                        runtime_status="incomplete",
                        capability_id=None,
                    ),
                )
            )
        await sequencer.transition_run(
            target_status="failed",
            event=self._runtime_event_draft(
                "run.failed",
                RunFailedPayload(
                    status="failed",
                    code="RUN_RECOVERY_EXHAUSTED",
                    message="本次回复连续恢复失败，请重新发送。",
                    retryable=True,
                ),
            ),
            updated_at=datetime.now(UTC),
            error_code="RUN_RECOVERY_EXHAUSTED",
            error_message="本次回复连续恢复失败，请重新发送。",
        )
        await self._session_service.touch_session(session_id=run.session_id)
        log_business_event(
            logger,
            "持久Run恢复次数耗尽",
            level=logging.ERROR,
            run_id=run.run_id,
            request_id=run.request_id,
            session_id=run.session_id,
            message_id=run.response_message_id,
            status="failed",
            recovery_attempt=run.recovery_attempts,
            error_code="RUN_RECOVERY_EXHAUSTED",
        )

    async def _finalize_persistent_cancellation_locked(
        self,
        run: Run,
        *,
        partial_content: str = "",
        capability_id: CapabilityId | None = None,
        turn: PreparedChatTurn | None = None,
    ) -> Run:
        """先保存非空 stopped 公共消息，再原子提交唯一取消终态事件。"""

        event_repository = self._runtime_event_repository
        if event_repository is None:
            raise ChatRuntimeError(
                code="RUNTIME_EVENT_SERVICE_UNAVAILABLE",
                message="RuntimeEvent 服务尚未完成初始化",
                retryable=True,
            )
        if run.status == "cancelled":
            return run
        inspector = RunCheckpointInspector(parent_graph=self._parent_graph)
        snapshot = await inspector.latest_for_run(run)
        existing_final_message = (
            recovered_final_message(run, snapshot)
            if snapshot is not None
            else None
        )
        sequencer = RunSequencer.for_run(
            run_id=run.run_id,
            response_message_id=run.response_message_id,
            repository=event_repository,
            publisher=self._runtime_event_publisher,
        )
        if (
            existing_final_message is not None
            and existing_final_message.runtime_status == "stopped"
        ):
            return await self._project_stopped_cancellation_locked(
                run,
                final_message=existing_final_message,
                sequencer=sequencer,
            )
        if turn is None:
            turn = await self._turn_from_run(run)
        content = partial_content
        if not content.strip():
            content = _STOPPED_MESSAGE_FALLBACK
        await self.persist_runtime_message(
            turn,
            content,
            runtime_status="stopped",
            capability_id=capability_id,
            include_human=(
                not turn.is_regeneration
                and not turn.is_resume
                and run.status != "interrupted"
            ),
        )
        return await self._project_stopped_cancellation_locked(
            run,
            final_message=RecoveredFinalMessage(
                runtime_status="stopped",
                capability_id=capability_id,
            ),
            sequencer=sequencer,
        )

    async def _require_nonempty_persistent_message(
        self,
        *,
        run: Run,
        expected_status: CompletionStatus,
    ) -> None:
        """确认 Graph 已用预分配消息 ID 保存非空完成或不支持回复。"""

        parent_state = await self._parent_graph.aget_state(
            parent_thread_config(run.session_id)
        )
        final_message = next(
            (
                message
                for message in reversed(
                    parent_state.values.get("messages", [])
                )
                if isinstance(message, AIMessage)
                and str(message.id) == str(run.response_message_id)
            ),
            None,
        )
        if (
            final_message is None
            or final_message.additional_kwargs.get("runtime_status")
            != expected_status
            or not str(final_message.text).strip()
        ):
            raise ChatRuntimeError(
                code="CHAT_EMPTY_TERMINAL_MESSAGE",
                message="聊天运行未形成非空终态回复",
                retryable=False,
            )

    def _persistent_runtime_dependencies(
        self,
    ) -> tuple[PostgresRunRepository, RunCoordinator]:
        repository = self._persistent_run_repository
        coordinator = self._run_coordinator
        if repository is None or coordinator is None:
            raise ChatRuntimeError(
                code="RUN_SERVICE_UNAVAILABLE",
                message="Run 服务尚未完成初始化",
                retryable=True,
            )
        return repository, coordinator

    def _require_interrupt_repository(
        self,
    ) -> PostgresRunInterruptRepository:
        repository = self._run_interrupt_repository
        if repository is None:
            raise ChatRuntimeError(
                code="INTERRUPT_SERVICE_UNAVAILABLE",
                message="Interrupt 服务尚未完成初始化",
                retryable=True,
            )
        return repository

    async def _resolve_existing_request(
        self,
        *,
        repository: PostgresRunRepository,
        coordinator: RunCoordinator,
        request_id: UUID,
        fingerprint: str,
    ) -> Run | None:
        try:
            existing = await repository.get_by_request_id(request_id)
        except RunNotFoundError:
            return None
        if existing.request_fingerprint != fingerprint:
            raise RunRequestConflictError(
                code="RUN_REQUEST_CONFLICT",
                message="request_id 已用于不同请求",
                status_code=409,
            )
        await self._require_owned_session(existing.session_id)
        await coordinator.wake(existing.run_id)
        return existing

    async def _require_owned_session(self, session_id: UUID) -> Session:
        session = await self._session_repository.get(session_id)
        if session.user_id != self._settings.local_user_id:
            raise ChatSessionError(
                code="SESSION_NOT_FOUND",
                message="Session 不存在",
                status_code=404,
            )
        return session

    async def _get_owned_parent_state(self, session_id: UUID):
        await self._require_owned_session(session_id)
        state = await self._parent_graph.aget_state(
            parent_thread_config(session_id)
        )
        if not state.values:
            raise ChatSessionError(
                code="SESSION_STATE_NOT_FOUND",
                message="Session 的 Parent 状态不存在",
                status_code=409,
            )
        return state

    async def _turn_from_run(
        self,
        run: Run,
        *,
        is_resume: bool = False,
        resume_payload: dict[str, JsonValue] | None = None,
        recovery_config: RunnableConfig | None = None,
    ) -> PreparedChatTurn:
        config = parent_thread_config(
            run.session_id,
            message_id=run.response_message_id,
            run_id=run.run_id,
            request_id=run.request_id,
            input_message_id=run.input_message_id,
            response_message_id=run.response_message_id,
        )
        configurable = config["configurable"]
        if (
            run.start_checkpoint_id is not None
            and not is_resume
            and recovery_config is None
        ):
            configurable["checkpoint_ns"] = ""
            configurable["checkpoint_id"] = run.start_checkpoint_id
        if recovery_config is not None:
            human_message = None
            config = recovery_config
        elif is_resume:
            human_message = None
        elif run.run_type == "regenerate":
            human_message = None
            if run.start_checkpoint_id is None:
                raise ChatRuntimeError(
                    code="RUN_INPUT_INVALID",
                    message="Regenerate Run 缺少起始 checkpoint",
                    retryable=False,
                )
            config = await self._checkpoint_forker.create_fork_from_checkpoint(
                session_id=run.session_id,
                start_checkpoint_id=run.start_checkpoint_id,
                response_message_id=run.response_message_id,
                run_id=run.run_id,
                request_id=run.request_id,
                input_message_id=run.input_message_id,
            )
        else:
            try:
                content = str(run.input_payload["message"]["content"])
            except (KeyError, TypeError) as error:
                raise ChatRuntimeError(
                    code="RUN_INPUT_INVALID",
                    message="Run 恢复输入不完整",
                    retryable=False,
                ) from error
            human_message = HumanMessage(
                content=content,
                id=str(run.input_message_id),
            )
        return PreparedChatTurn(
            session_id=run.session_id,
            human_message=human_message,
            response_message_id=run.response_message_id,
            config=config,
            active_run=ActiveRun(
                session_id=run.session_id,
                response_message_id=run.response_message_id,
            ),
            is_regeneration=run.run_type == "regenerate",
            is_resume=is_resume,
            is_recovery=recovery_config is not None,
            resume_payload=resume_payload,
        )

    async def _pending_graph_interrupt(
        self,
        *,
        run: Run,
        turn: PreparedChatTurn,
    ) -> tuple[UUID, dict[str, JsonValue]] | None:
        """从已落盘的最新 Graph 快照投影唯一公开中断提示。"""

        snapshot = await self._parent_graph.aget_state(
            parent_thread_config(
                run.session_id,
                message_id=run.response_message_id,
            )
        )
        return self._graph_interrupt_from_snapshot(run=run, snapshot=snapshot)

    def _graph_interrupt_from_snapshot(
        self,
        *,
        run: Run,
        snapshot: StateSnapshot,
    ) -> tuple[UUID, dict[str, JsonValue]] | None:
        """校验精确快照并投影唯一公开中断提示。"""

        raw_interrupts = tuple(snapshot.interrupts)
        if not raw_interrupts:
            return None
        if len(raw_interrupts) != 1:
            raise ChatRuntimeError(
                code="INTERRUPT_PARALLEL_UNSUPPORTED",
                message="Stage 2.5 不支持并行 Interrupt",
                status_code=409,
            )
        checkpoint_id = self._checkpoint_id(snapshot.config)
        if checkpoint_id is None:
            raise ChatRuntimeError(
                code="INTERRUPT_CHECKPOINT_INVALID",
                message="Interrupt 缺少已持久化 checkpoint 标识",
                retryable=True,
            )
        raw_interrupt = raw_interrupts[0]
        value = raw_interrupt.value
        if isinstance(value, str):
            prompt = value.strip()
        elif (
            isinstance(value, dict)
            and set(value) == {"prompt"}
            and isinstance(value.get("prompt"), str)
        ):
            prompt = value["prompt"].strip()
        else:
            raise ChatRuntimeError(
                code="INTERRUPT_PAYLOAD_INVALID",
                message="Interrupt 只允许公开单个中文提示文本",
                status_code=409,
            )
        if not prompt or len(prompt) > 4000:
            raise ChatRuntimeError(
                code="INTERRUPT_PAYLOAD_INVALID",
                message="Interrupt 提示长度必须在 1 到 4000 个字符之间",
                status_code=409,
            )
        interrupt_id = uuid5(
            run.run_id,
            f"{checkpoint_id}:{raw_interrupt.id}",
        )
        return interrupt_id, {"prompt": prompt}

    @staticmethod
    def _checkpoint_id(config: RunnableConfig) -> str | None:
        configurable = config.get("configurable", {})
        checkpoint_id = configurable.get("checkpoint_id")
        return str(checkpoint_id) if checkpoint_id is not None else None

    @staticmethod
    def _runtime_event_draft(event_type: str, payload) -> RuntimeEventDraft:
        return RuntimeEventDraft(
            event_type=event_type,
            source="runtime.executor",
            visibility="public",
            payload=payload,
            schema_version=1,
            durability=(
                "transient" if event_type == "message.delta" else "durable"
            ),
            created_at=datetime.now(UTC),
        )

    @staticmethod
    def _internal_runtime_event_draft(
        event_type: str,
        payload,
    ) -> RuntimeEventDraft:
        """构造不向 SSE 投影、但必须随 Run 状态原子落库的内部事件。"""

        return RuntimeEventDraft(
            event_type=event_type,
            source="runtime.api",
            visibility="internal",
            payload=payload,
            schema_version=1,
            durability="durable",
            created_at=datetime.now(UTC),
        )

    async def prepare_turn(
        self,
        *,
        session_id: UUID | None,
        content: str,
    ) -> PreparedChatTurn:
        """在建立 SSE 前创建或校验 Session，并分配本轮稳定消息 UUID。"""

        response_message_id = self._response_message_id_factory()
        log_business_event(
            logger,
            "聊天轮次准备开始",
            session_id=session_id,
            message_id=response_message_id,
            session_mode="existing" if session_id is not None else "new",
        )
        if session_id is not None:
            session = await self._session_repository.get(session_id)
            if session.user_id != self._settings.local_user_id:
                raise ChatSessionError(
                    code="SESSION_NOT_FOUND",
                    message="Session 不存在",
                    status_code=404,
                )
            parent_state = await self._parent_graph.aget_state(
                parent_thread_config(session_id)
            )
            if not parent_state.values:
                raise ChatSessionError(
                    code="SESSION_STATE_NOT_FOUND",
                    message="Session 的 Parent 状态不存在",
                    status_code=409,
                )

        reserved_session_id = session_id or self._session_id_factory()
        active_run = await self._run_registry.reserve(
            reserved_session_id,
            response_message_id,
        )
        try:
            human_message_id = self._human_message_id_factory()
            if session_id is None:
                started = await self._session_service.prepare_new_session(
                    session_id=reserved_session_id,
                    human_message_id=human_message_id,
                    content=content,
                )
                human_message = started.human_message
            else:
                human_message = HumanMessage(
                    content=content,
                    id=str(human_message_id),
                )

            turn = PreparedChatTurn(
                session_id=reserved_session_id,
                human_message=human_message,
                response_message_id=response_message_id,
                config=parent_thread_config(
                    reserved_session_id,
                    message_id=response_message_id,
                ),
                active_run=active_run,
            )
            self._prepared_turns[reserved_session_id] = turn
            log_business_event(
                logger,
                "聊天轮次准备完成",
                session_id=reserved_session_id,
                human_message_id=human_message.id,
                message_id=response_message_id,
                session_mode="existing" if session_id is not None else "new",
            )
            return turn
        except BaseException as error:
            log_business_event(
                logger,
                "聊天轮次准备失败",
                level=logging.ERROR,
                session_id=reserved_session_id,
                message_id=response_message_id,
                error_type=type(error).__name__,
                error_code=(
                    error.code if isinstance(error, ApplicationError) else None
                ),
            )
            await self._run_registry.release(active_run)
            raise

    async def list_sessions(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> SessionPage:
        """返回固定本地用户的一页 Session 产品数据。"""

        return await self._session_service.list_sessions(
            cursor=cursor,
            limit=limit,
        )

    async def rename_session(
        self,
        *,
        session_id: UUID,
        title: str,
    ) -> Session:
        """更新固定本地用户拥有的 Session 标题。"""

        return await self._session_service.rename_session(
            session_id=session_id,
            title=title,
        )

    async def list_messages(
        self,
        *,
        session_id: UUID,
        before: UUID | None,
        limit: int,
    ) -> MessagePage:
        """返回固定本地用户 Session 的当前活动分支消息。"""

        return await self._history_adapter.list_messages(
            session_id=session_id,
            before=before,
            limit=limit,
        )

    async def submit_feedback(
        self,
        *,
        message_id: UUID,
        action: FeedbackAction,
    ) -> FeedbackResult:
        """保存、替换或取消当前活动完成回答的反馈。"""

        log_business_event(
            logger,
            "消息反馈开始",
            message_id=message_id,
            action=action,
        )
        if self._feedback_service is None:
            raise ChatRuntimeError(
                code="MESSAGE_FEEDBACK_UNAVAILABLE",
                message="消息反馈服务尚未完成初始化",
                retryable=True,
            )
        result = await self._feedback_service.submit(
            message_id=message_id,
            action=action,
        )
        log_business_event(
            logger,
            "消息反馈完成",
            message_id=message_id,
            action=action,
            feedback=result.feedback,
        )
        return result

    async def prepare_regeneration(
        self,
        *,
        session_id: UUID,
        message_id: UUID,
    ) -> PreparedChatTurn:
        """占用 Session，并从目标完成回答之前创建 Parent fork。"""

        log_business_event(
            logger,
            "重新生成准备开始",
            session_id=session_id,
            source_message_id=message_id,
        )
        session = await self._session_repository.get(session_id)
        if session.user_id != self._settings.local_user_id:
            raise ChatSessionError(
                code="SESSION_NOT_FOUND",
                message="Session 不存在",
                status_code=404,
            )

        response_message_id = self._response_message_id_factory()
        active_run = await self._run_registry.reserve(
            session_id,
            response_message_id,
        )
        preparation_done = asyncio.get_running_loop().create_future()
        self._turn_preparation_barriers[session_id] = (
            active_run,
            preparation_done,
        )
        fork_task: asyncio.Task[RunnableConfig] | None = None
        turn: PreparedChatTurn | None = None
        cancelled_error: asyncio.CancelledError | None = None
        try:
            fork_task = asyncio.create_task(
                self._checkpoint_forker.create_fork(
                    session_id=session_id,
                    message_id=message_id,
                    response_message_id=response_message_id,
                ),
                name=f"regeneration-fork:{session_id}",
            )
            while True:
                try:
                    config = await asyncio.shield(fork_task)
                    break
                except asyncio.CancelledError as error:
                    if fork_task.done():
                        if fork_task.cancelled():
                            raise
                        config = fork_task.result()
                        cancelled_error = cancelled_error or error
                        break
                    cancelled_error = cancelled_error or error
            turn = PreparedChatTurn(
                session_id=session_id,
                human_message=None,
                response_message_id=response_message_id,
                config=config,
                active_run=active_run,
                is_regeneration=True,
            )
            self._prepared_turns[session_id] = turn
            self._finish_turn_preparation(active_run)
            if cancelled_error is not None:
                try:
                    await self.cancel_run(active_run, reason="disconnected")
                finally:
                    raise cancelled_error
            log_business_event(
                logger,
                "重新生成准备完成",
                session_id=session_id,
                source_message_id=message_id,
                message_id=response_message_id,
            )
            return turn
        except BaseException as error:
            log_business_event(
                logger,
                "重新生成准备失败",
                level=logging.ERROR,
                session_id=session_id,
                source_message_id=message_id,
                message_id=response_message_id,
                error_type=type(error).__name__,
                error_code=(
                    error.code if isinstance(error, ApplicationError) else None
                ),
            )
            if turn is None:
                if fork_task is not None and not fork_task.done():
                    fork_task.cancel()
                    await asyncio.gather(fork_task, return_exceptions=True)
                if isinstance(error, ApplicationError):
                    active_run._terminal_error = ChatRuntimeError(
                        code=error.code,
                        message=error.message,
                        status_code=error.status_code,
                        retryable=error.retryable,
                    )
                else:
                    active_run._terminal_error = ChatRuntimeError(
                        code="MESSAGE_REGENERATE_FORK_FAILED",
                        message="创建消息重新生成分支失败",
                        retryable=True,
                    )
                await self._run_registry.release(active_run)
            raise
        finally:
            self._finish_turn_preparation(active_run)

    async def stop_session(
        self,
        *,
        session_id: UUID,
    ) -> Literal["stopped", "idle"]:
        """停止固定本地用户 Session 的当前 Run，并等待终态清理。"""

        log_business_event(logger, "停止Session开始", session_id=session_id)
        session = await self._session_repository.get(session_id)
        if session.user_id != self._settings.local_user_id:
            raise ChatSessionError(
                code="SESSION_NOT_FOUND",
                message="Session 不存在",
                status_code=404,
            )
        run = await self._run_registry.get_active(session_id)
        if run is None:
            log_business_event(
                logger,
                "停止Session完成",
                session_id=session_id,
                status="idle",
            )
            return "idle"
        accepted = await self.cancel_run(run, reason="stopped")
        if accepted and run.cancel_reason == "stopped":
            log_business_event(
                logger,
                "停止Session完成",
                session_id=session_id,
                message_id=run.response_message_id,
                status="stopped",
            )
            return "stopped"
        log_business_event(
            logger,
            "停止Session完成",
            session_id=session_id,
            message_id=run.response_message_id,
            status="idle",
        )
        return "idle"

    async def delete_session(self, *, session_id: UUID) -> None:
        """幂等停止并硬删除固定本地用户的 Session。"""

        log_business_event(logger, "删除Session开始", session_id=session_id)
        async with self._run_registry.deleting(session_id) as deletion:
            try:
                session = await self._session_repository.get(session_id)
            except SessionNotFoundError:
                log_business_event(
                    logger,
                    "删除Session完成",
                    session_id=session_id,
                    status="not_found",
                )
                return
            except Exception as error:
                raise SessionDeletionError(
                    code="SESSION_DELETE_FAILED",
                    message="Session 关联数据删除失败，请重试",
                    status_code=500,
                    retryable=True,
                ) from error

            if session.user_id != self._settings.local_user_id:
                log_business_event(
                    logger,
                    "删除Session完成",
                    session_id=session_id,
                    status="not_owned",
                )
                return

            persistent_run_repository = self._persistent_run_repository
            run_coordinator = self._run_coordinator
            if (
                persistent_run_repository is not None
                and run_coordinator is not None
            ):
                active_persistent_run = (
                    await persistent_run_repository.get_active_for_session(
                        session_id
                    )
                )
                if active_persistent_run is not None:
                    await self.cancel_persistent_run(
                        run_id=active_persistent_run.run_id
                    )
                    await run_coordinator.wait_for_run(
                        active_persistent_run.run_id
                    )
                    persisted_terminal = await persistent_run_repository.get(
                        active_persistent_run.run_id
                    )
                    if persisted_terminal.status not in {
                        "completed",
                        "failed",
                        "cancelled",
                    }:
                        raise SessionDeletionError(
                            code="SESSION_DELETE_FAILED",
                            message="Session 活动 Run 尚未完成取消，请重试",
                            status_code=500,
                            retryable=True,
                        )

            run = await self._run_registry.get_active(session_id)
            try:
                if run is not None:
                    await self.cancel_run(run, reason="stopped")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise SessionDeletionError(
                    code="SESSION_DELETE_FAILED",
                    message="Session 关联数据删除失败，请重试",
                    status_code=500,
                    retryable=True,
                ) from error

            if self._deletion_service is None:
                raise SessionDeletionError(
                    code="SESSION_DELETE_FAILED",
                    message="Session 关联数据删除失败，请重试",
                    status_code=500,
                    retryable=True,
                )
            persisted_deletion = asyncio.create_task(
                self._deletion_service.delete_persisted_data(
                    session_id=session_id
                ),
                name=f"session-delete:{session_id}",
            )
            try:
                await asyncio.shield(persisted_deletion)
            except asyncio.CancelledError:
                await persisted_deletion
                deletion.mark_deleted()
                raise
            deletion.mark_deleted()
        log_business_event(
            logger,
            "删除Session完成",
            session_id=session_id,
            status="deleted",
        )

    async def stream_turn(
        self,
        turn: PreparedChatTurn,
    ) -> AsyncIterator[BaseMessage]:
        """只转发 Parent custom stream 中的公共消息事件。"""

        if turn.is_resume:
            if turn.resume_payload is None:
                raise ChatRuntimeError(
                    code="INTERRUPT_RESUME_INPUT_INVALID",
                    message="Interrupt 恢复输入不能为空",
                    status_code=409,
                )
            graph_input = Command(resume=turn.resume_payload)
        elif turn.is_recovery:
            graph_input = None
        else:
            graph_input = (
                None
                if turn.is_regeneration
                else {"messages": [turn.human_message]}
            )
        async for event in self._parent_graph.astream(
            graph_input,
            turn.config,
            stream_mode="custom",
        ):
            if not isinstance(event, BaseMessage):
                raise ChatRuntimeError(
                    code="CHAT_INVALID_STREAM_EVENT",
                    message="Parent Graph 返回了非法流式消息事件",
                )
            yield event

    async def start_producer(self, turn: PreparedChatTurn) -> None:
        """为已占用的 Run 启动独立 Graph producer task。"""

        log_business_event(
            logger,
            "聊天Producer启动",
            session_id=turn.session_id,
            message_id=turn.response_message_id,
            is_regeneration=turn.is_regeneration,
        )
        task = asyncio.create_task(
            self._produce_turn(turn),
            name=f"chat-producer:{turn.session_id}",
        )
        try:
            attached = await self._run_registry.attach_producer(
                turn.active_run,
                task,
            )
            if not attached:
                await asyncio.gather(task, return_exceptions=True)
            else:
                task.add_done_callback(
                    lambda completed_task: self._recover_prestart_cancellation(
                        turn,
                        completed_task,
                    )
                )
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    def _recover_prestart_cancellation(
        self,
        turn: PreparedChatTurn,
        completed_task: asyncio.Task[None],
    ) -> None:
        """任务在进入 producer 前已取消时，补启动唯一的取消终态处理。"""

        if (
            not completed_task.cancelled()
            or turn.active_run.terminal_future.done()
        ):
            return
        asyncio.create_task(
            self._finalize_unstarted_run(turn.active_run),
            name=f"run-prestart-finalizer:{turn.session_id}",
        )

    async def _produce_turn(self, turn: PreparedChatTurn) -> None:
        """执行 Parent Graph，并把产品事件写入本轮专用队列。"""

        partial_text: list[str] = []
        terminal_events: list[ProductRunEvent] = []
        capability_id: CapabilityId | None = None
        cancelled = False
        terminal_status: str | None = None
        run_started_at = perf_counter()
        log_business_event(
            logger,
            "聊天运行开始",
            session_id=turn.session_id,
            message_id=turn.response_message_id,
            is_regeneration=turn.is_regeneration,
        )
        try:
            try:
                async for message in self.stream_turn(turn):
                    capability_id = self._message_capability_id(
                        message,
                        fallback=capability_id,
                    )
                    delta = str(message.text)
                    if not delta:
                        continue
                    partial_text.append(delta)
                    await turn.active_run.event_queue.put(
                        ProductRunEvent(
                            name="message",
                            data=MessageEventData(
                                session_id=turn.session_id,
                                message_id=turn.response_message_id,
                                capability_id=capability_id,
                                delta=delta,
                            ),
                        )
                    )
                completion_status = await self.get_completion_status(turn)
                terminal_status = completion_status
                terminal_events.append(
                    ProductRunEvent(
                        name="done",
                        data=DoneEventData(
                            session_id=turn.session_id,
                            message_id=turn.response_message_id,
                            capability_id=capability_id,
                            status=completion_status,
                        ),
                    )
                )
            except Exception as error:
                reported_error: Exception = error
                if partial_text:
                    try:
                        await self.persist_incomplete(
                            turn,
                            "".join(partial_text),
                            capability_id=capability_id,
                        )
                    except Exception:
                        reported_error = ApplicationError(
                            code="CHAT_INCOMPLETE_PERSIST_FAILED",
                            message="未能保存模型的部分输出",
                            retryable=False,
                        )
                terminal_status = "failed"
                log_business_event(
                    logger,
                    "聊天运行失败",
                    level=logging.ERROR,
                    session_id=turn.session_id,
                    message_id=turn.response_message_id,
                    capability_id=capability_id,
                    error_type=type(reported_error).__name__,
                    error_code=(
                        reported_error.code
                        if isinstance(reported_error, ApplicationError)
                        else None
                    ),
                )
                terminal_events = self._failed_terminal_events(
                    session_id=turn.session_id,
                    message_id=turn.response_message_id,
                    capability_id=capability_id,
                    error=reported_error,
                )
            await self._run_registry.begin_finalization(turn.active_run)
        except asyncio.CancelledError:
            cancelled = True
            await self._run_registry.begin_finalization(turn.active_run)
            reason = turn.active_run.cancel_reason or "disconnected"
            runtime_status: Literal["stopped", "incomplete"] = (
                "stopped" if reason == "stopped" else "incomplete"
            )
            terminal_status = runtime_status
            try:
                await self.persist_runtime_message(
                    turn,
                    "".join(partial_text),
                    runtime_status=runtime_status,
                    capability_id=capability_id,
                )
            except Exception:
                terminal_error = ChatRuntimeError(
                    code=(
                        "CHAT_STOP_PERSIST_FAILED"
                        if runtime_status == "stopped"
                        else "CHAT_INCOMPLETE_PERSIST_FAILED"
                    ),
                    message=(
                        "未能保存停止后的回复"
                        if runtime_status == "stopped"
                        else "未能保存模型的不完整输出"
                    ),
                    retryable=False,
                )
                turn.active_run._terminal_error = terminal_error
                terminal_status = "failed"
                terminal_events = self._failed_terminal_events(
                    session_id=turn.session_id,
                    message_id=turn.response_message_id,
                    capability_id=capability_id,
                    error=terminal_error,
                )
            else:
                terminal_events = (
                    [
                        ProductRunEvent(
                            name="done",
                            data=DoneEventData(
                                session_id=turn.session_id,
                                message_id=turn.response_message_id,
                                capability_id=capability_id,
                                status="stopped",
                            ),
                        )
                    ]
                    if runtime_status == "stopped"
                    else []
                )
            log_business_event(
                logger,
                "聊天运行取消",
                session_id=turn.session_id,
                message_id=turn.response_message_id,
                capability_id=capability_id,
                cancel_reason=reason,
                status=terminal_status,
            )

        finalize_cancelled = await self._finalize_run(
            turn=turn,
            terminal_events=terminal_events,
            capability_id=capability_id,
            cancelled=False,
        )
        cancelled = cancelled or finalize_cancelled
        final_status = (
            "failed"
            if turn.active_run._terminal_error is not None
            else terminal_status
        )
        log_business_event(
            logger,
            "聊天运行结束",
            session_id=turn.session_id,
            message_id=turn.response_message_id,
            capability_id=capability_id,
            status=final_status,
            output_chars=sum(len(part) for part in partial_text),
            duration_ms=round((perf_counter() - run_started_at) * 1000, 2),
        )

        if cancelled:
            raise asyncio.CancelledError

    async def _finalize_run(
        self,
        *,
        turn: PreparedChatTurn,
        terminal_events: list[ProductRunEvent],
        capability_id: CapabilityId | None,
        cancelled: bool,
    ) -> bool:
        """刷新 Session，并在任何终态下结束事件队列和释放占用。"""

        touch_task = asyncio.create_task(
            self._session_service.touch_session(session_id=turn.session_id),
            name=f"session-touch:{turn.session_id}",
        )
        try:
            try:
                await asyncio.shield(touch_task)
            except asyncio.CancelledError:
                cancelled = True
                terminal_events.clear()
                try:
                    await touch_task
                except Exception:
                    pass
            except Exception:
                if not cancelled:
                    terminal_error = ApplicationError(
                        code="CHAT_SESSION_TOUCH_FAILED",
                        message="未能更新 Session 的活跃时间",
                        retryable=False,
                    )
                    if turn.active_run._terminal_error is None:
                        turn.active_run._terminal_error = terminal_error
                        terminal_events = self._failed_terminal_events(
                            session_id=turn.session_id,
                            message_id=turn.response_message_id,
                            capability_id=capability_id,
                            error=terminal_error,
                        )

            if not cancelled:
                for event in terminal_events:
                    await turn.active_run.event_queue.put(event)
        finally:
            cleanup_cancelled = await self._await_run_cleanup(turn.active_run)
            cancelled = cancelled or cleanup_cancelled
        return cancelled

    async def _await_run_cleanup(self, run: ActiveRun) -> bool:
        """在独立任务中结束队列并释放 Registry，避免并发取消打断。"""

        cleanup_task = asyncio.create_task(
            self._complete_run_cleanup(run),
            name=f"run-cleanup:{run.session_id}",
        )
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task
            return True
        return False

    async def _complete_run_cleanup(self, run: ActiveRun) -> None:
        """发送队列终止标记，并以 reservation token 安全释放 Run。"""

        await run.event_queue.put(None)
        await self._run_registry.release(run)
        turn = self._prepared_turns.get(run.session_id)
        if turn is not None and turn.active_run is run:
            del self._prepared_turns[run.session_id]

    @staticmethod
    def _failed_terminal_events(
        *,
        session_id: UUID,
        message_id: UUID,
        capability_id: CapabilityId | None,
        error: Exception,
    ) -> list[ProductRunEvent]:
        """把内部异常转换为不泄漏实现细节的产品失败终态。"""

        if isinstance(error, ApplicationError):
            error_data = ErrorEventData(
                session_id=session_id,
                code=error.code,
                message=error.message,
                retryable=error.retryable,
            )
        else:
            error_data = ErrorEventData(
                session_id=session_id,
                code="CHAT_RUNTIME_FAILED",
                message="聊天运行失败",
                retryable=False,
            )
        return [
            ProductRunEvent(name="error", data=error_data),
            ProductRunEvent(
                name="done",
                data=DoneEventData(
                    session_id=session_id,
                    message_id=message_id,
                    capability_id=capability_id,
                    status="failed",
                ),
            ),
        ]

    async def cancel_run(
        self,
        run: ActiveRun,
        *,
        reason: CancelReason,
    ) -> bool:
        """取消并等待指定响应对应的 producer 完成清理。"""

        accepted = await self._run_registry.request_cancel(
            run,
            reason,
            unstarted_finalizer=lambda: self._finalize_unstarted_run(run),
        )
        if accepted and run._terminal_error is not None:
            raise run._terminal_error
        return accepted

    async def _finalize_unstarted_run(self, run: ActiveRun) -> None:
        """为尚未启动 producer 的取消请求保存可解释终态并释放占用。"""

        terminal_events: list[ProductRunEvent] = []
        try:
            await self._wait_for_turn_preparation(run)
            if not await self._run_registry.begin_finalization(run):
                return
            turn = self._prepared_turns.get(run.session_id)
            if turn is not None and turn.active_run is run:
                runtime_status: Literal["stopped", "incomplete"] = (
                    "stopped"
                    if run.cancel_reason == "stopped"
                    else "incomplete"
                )
                try:
                    await self.persist_runtime_message(
                        turn,
                        "",
                        runtime_status=runtime_status,
                        capability_id=None,
                        include_human=not turn.is_regeneration,
                    )
                    if runtime_status == "stopped":
                        terminal_events.append(
                            ProductRunEvent(
                                name="done",
                                data=DoneEventData(
                                    session_id=turn.session_id,
                                    message_id=turn.response_message_id,
                                    capability_id=None,
                                    status="stopped",
                                ),
                            )
                        )
                except Exception:
                    terminal_error = ChatRuntimeError(
                        code=(
                            "CHAT_STOP_PERSIST_FAILED"
                            if runtime_status == "stopped"
                            else "CHAT_INCOMPLETE_PERSIST_FAILED"
                        ),
                        message=(
                            "未能保存停止后的回复"
                            if runtime_status == "stopped"
                            else "未能保存模型的不完整输出"
                        ),
                        retryable=False,
                    )
                    run._terminal_error = terminal_error
                    for event in self._failed_terminal_events(
                        session_id=turn.session_id,
                        message_id=turn.response_message_id,
                        capability_id=None,
                        error=terminal_error,
                    ):
                        terminal_events.append(event)
            try:
                await self._session_service.touch_session(
                    session_id=run.session_id
                )
            except Exception:
                if run._terminal_error is None:
                    terminal_error = ApplicationError(
                        code="CHAT_SESSION_TOUCH_FAILED",
                        message="未能更新 Session 的活跃时间",
                        retryable=False,
                    )
                    run._terminal_error = terminal_error
                    terminal_events = self._failed_terminal_events(
                        session_id=run.session_id,
                        message_id=run.response_message_id,
                        capability_id=None,
                        error=terminal_error,
                    )
            for event in terminal_events:
                await run.event_queue.put(event)
        finally:
            cleanup_cancelled = await self._await_run_cleanup(run)
            if cleanup_cancelled:
                raise asyncio.CancelledError

    async def _wait_for_turn_preparation(self, run: ActiveRun) -> None:
        """等待 reservation 对应的完整 turn 发布，避免提前完成 Stop。"""

        pending = self._turn_preparation_barriers.get(run.session_id)
        if pending is not None and pending[0] is run:
            await asyncio.shield(pending[1])

    def _finish_turn_preparation(self, run: ActiveRun) -> None:
        """原子发布准备完成信号，并忽略已被替代的旧 Run。"""

        pending = self._turn_preparation_barriers.get(run.session_id)
        if pending is None or pending[0] is not run:
            return
        del self._turn_preparation_barriers[run.session_id]
        if not pending[1].done():
            pending[1].set_result(None)

    async def close(self) -> None:
        """关闭服务前取消并等待仍在运行的全部 producer。"""

        await self._run_registry.close(
            unstarted_finalizer=self._finalize_unstarted_run
        )

    async def get_completion_status(
        self,
        turn: PreparedChatTurn,
    ) -> CompletionStatus:
        """从 Parent 最终状态读取本轮 completed 或 unsupported。"""

        parent_state = await self._parent_graph.aget_state(
            parent_thread_config(turn.session_id)
        )
        completion_status = parent_state.values.get("completion_status")
        if completion_status not in ("completed", "unsupported"):
            raise ChatRuntimeError(
                code="CHAT_COMPLETION_STATUS_INVALID",
                message="Parent Graph 缺少合法的本轮完成状态",
            )
        return cast(CompletionStatus, completion_status)

    async def persist_incomplete(
        self,
        turn: PreparedChatTurn,
        content: str,
        *,
        capability_id: CapabilityId | None = None,
    ) -> None:
        """把已发送的部分文本作为 incomplete AIMessage 写回 Parent。"""

        await self.persist_runtime_message(
            turn,
            content,
            runtime_status="incomplete",
            capability_id=capability_id,
        )

    async def persist_runtime_message(
        self,
        turn: PreparedChatTurn,
        content: str,
        *,
        runtime_status: Literal["incomplete", "stopped"],
        capability_id: CapabilityId | None,
        include_human: bool = False,
    ) -> None:
        """把非 completed 的公共 AIMessage 终态写回 Parent 权威历史。"""

        message = AIMessage(
            content=content,
            id=str(turn.response_message_id),
            additional_kwargs={
                "runtime_status": runtime_status,
                "capability_id": capability_id,
            },
        )
        messages: list[BaseMessage] = [message]
        if include_human:
            if turn.human_message is None:
                raise ChatRuntimeError(
                    code="CHAT_HUMAN_MESSAGE_MISSING",
                    message="普通聊天终态缺少 HumanMessage",
                )
            messages.insert(0, turn.human_message)
        await self._parent_graph.aupdate_state(
            turn.config,
            {"messages": messages, "completion_status": None},
            as_node="invoke_capability",
        )
        log_business_event(
            logger,
            "运行终态持久化完成",
            session_id=turn.session_id,
            message_id=turn.response_message_id,
            capability_id=capability_id,
            status=runtime_status,
            output_chars=len(content),
            include_human=include_human,
        )

    @staticmethod
    def _message_capability_id(
        message: BaseMessage,
        *,
        fallback: CapabilityId | None,
    ) -> CapabilityId | None:
        """从公共消息元数据读取 Stage 2 允许的能力标识。"""

        capability_id = message.additional_kwargs.get("capability_id")
        if capability_id in ("general_chat", "en_to_zh"):
            return cast(CapabilityId, capability_id)
        return fallback


@asynccontextmanager
async def open_chat_service(
    settings: Settings,
    *,
    model: BaseChatModel | None = None,
) -> AsyncIterator[ChatService]:
    """初始化运行所需数据库结构，并组装固定 Parent/Child 链路。"""

    log_business_event(logger, "聊天服务初始化开始")
    session_repository = PostgresSessionRepository(settings)
    await session_repository.setup()
    feedback_store = PostgresFeedbackStore(settings)
    await feedback_store.setup()
    run_repository = PostgresRunRepository(settings)
    await run_repository.setup()
    runtime_event_repository = PostgresRuntimeEventRepository(settings)
    await runtime_event_repository.setup()
    run_interrupt_repository = PostgresRunInterruptRepository(settings)
    runtime_event_publisher = RedisStreamPublisher.from_url(
        settings.redis_connection_string,
        ttl_seconds=settings.redis_stream_ttl_seconds,
        socket_timeout_seconds=settings.redis_socket_timeout_seconds,
    )
    runtime_event_reader = RedisStreamReader.from_url(
        settings.redis_connection_string,
        socket_timeout_seconds=settings.redis_socket_timeout_seconds,
    )
    runtime_event_gateway = RuntimeEventGateway(
        run_repository=run_repository,
        event_repository=runtime_event_repository,
        redis_reader=runtime_event_reader,
    )
    run_coordinator = RunCoordinator(
        repository=run_repository,
        user_id=settings.local_user_id,
        scan_interval_seconds=settings.run_coordinator_scan_interval_seconds,
        cancel_grace_seconds=settings.run_cancel_grace_seconds,
    )
    parent_state_store = PostgresParentStateStore(settings)
    async with open_stage_one_checkpointers(settings) as checkpointers:
        if settings.runtime_demo_mode:
            parent_graph = build_runtime_demo_graph(
                checkpointer=checkpointers.parent
            )
            log_business_event(
                logger,
                "Runtime开发演示模式已启用",
                runtime_demo_mode=True,
            )
        else:
            runtime_model = model or build_chat_model(settings)
            router = StageOneRouter(runtime_model)
            general_chat = GeneralChatAdapter(
                capability=GeneralChatCapability(
                    scope_model=runtime_model,
                    response_model=runtime_model,
                    checkpointer=checkpointers.general_chat,
                )
            )
            en_to_zh = EnglishToChineseAdapter(
                capability=EnglishToChineseCapability(
                    scope_model=runtime_model,
                    translation_model=runtime_model,
                    checkpointer=checkpointers.en_to_zh,
                )
            )

            async def invoke_capability(state, config):
                """只在两个固定 Stage 1 Adapter 之间分发。"""

                configured_run_id = run_id_from_parent_config(config)

                async def cancellation_probe() -> bool:
                    return (
                        configured_run_id is not None
                        and await run_coordinator.is_cancel_requested(
                            configured_run_id
                        )
                    )

                if state["resolved_capability_id"] == "general_chat":
                    return await general_chat.invoke(
                        state,
                        config,
                        cancellation_probe=cancellation_probe,
                    )
                if state["resolved_capability_id"] == "en_to_zh":
                    return await en_to_zh.invoke(
                        state,
                        config,
                        cancellation_probe=cancellation_probe,
                    )
                raise ChatRuntimeError(
                    code="CHAT_CAPABILITY_INVALID",
                    message="Parent Graph 选择了非法的 Stage 1 能力",
                )

            parent_graph = build_parent_graph(
                route=router.route,
                invoke_capability=invoke_capability,
                checkpointer=checkpointers.parent,
            )
        session_service = SessionService(
            settings=settings,
            session_repository=session_repository,
            parent_state_store=parent_state_store,
        )
        history_adapter = MessageHistoryAdapter(
            settings=settings,
            session_repository=session_repository,
            parent_state_store=parent_state_store,
            feedback_store=feedback_store,
        )
        run_registry = ActiveRunRegistry()
        feedback_service = FeedbackService(
            settings=settings,
            target_finder=history_adapter,
            store=feedback_store,
            operation_coordinator=run_registry,
        )
        deletion_service = SessionDeletionService(
            settings=settings,
            parent_checkpointer=checkpointers.parent,
            general_chat_checkpointer=checkpointers.general_chat,
            en_to_zh_checkpointer=checkpointers.en_to_zh,
            feedback_store=feedback_store,
            session_repository=session_repository,
            runtime_store=run_repository,
            redis_stream_cleaner=runtime_event_publisher,
        )
        checkpoint_forker = CheckpointForker(
            history_adapter=history_adapter,
            parent_graph=parent_graph,
        )
        service = ChatService(
            settings=settings,
            session_repository=session_repository,
            session_service=session_service,
            parent_graph=parent_graph,
            history_adapter=history_adapter,
            feedback_service=feedback_service,
            deletion_service=deletion_service,
            checkpoint_forker=checkpoint_forker,
            run_registry=run_registry,
            persistent_run_repository=run_repository,
            runtime_event_repository=runtime_event_repository,
            runtime_event_publisher=runtime_event_publisher,
            runtime_event_gateway=runtime_event_gateway,
            run_interrupt_repository=run_interrupt_repository,
            run_coordinator=run_coordinator,
        )
        try:
            await run_coordinator.start(service.execute_persistent_run)
            log_business_event(logger, "聊天服务初始化完成")
            yield service
        finally:
            log_business_event(logger, "聊天服务关闭开始")
            await run_coordinator.close()
            await service.close()
            await runtime_event_publisher.aclose()
            await runtime_event_reader.aclose()
            log_business_event(logger, "聊天服务关闭完成")
