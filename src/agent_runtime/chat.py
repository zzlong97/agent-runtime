"""聊天应用层，连接 Session 产品能力、Active Run 与 Parent Graph。"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

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
from agent_runtime.graph.config import parent_thread_config
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
from agent_runtime.runtime.repository import PostgresRunRepository
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
        session_id_factory: Callable[[], UUID] = uuid4,
        human_message_id_factory: Callable[[], UUID] = uuid4,
        response_message_id_factory: Callable[[], UUID] = uuid4,
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
        self._session_id_factory = session_id_factory
        self._human_message_id_factory = human_message_id_factory
        self._response_message_id_factory = response_message_id_factory
        self._prepared_turns: dict[UUID, PreparedChatTurn] = {}
        self._turn_preparation_barriers: dict[
            UUID,
            tuple[ActiveRun, asyncio.Future[None]],
        ] = {}

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
    parent_state_store = PostgresParentStateStore(settings)
    runtime_model = model or build_chat_model(settings)

    async with open_stage_one_checkpointers(settings) as checkpointers:
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

            if state["resolved_capability_id"] == "general_chat":
                return await general_chat.invoke(state, config)
            if state["resolved_capability_id"] == "en_to_zh":
                return await en_to_zh.invoke(state, config)
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
        )
        try:
            log_business_event(logger, "聊天服务初始化完成")
            yield service
        finally:
            log_business_event(logger, "聊天服务关闭开始")
            await service.close()
            log_business_event(logger, "聊天服务关闭完成")
