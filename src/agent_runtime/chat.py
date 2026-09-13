"""聊天应用层，连接 Session 产品能力、Active Run 与 Parent Graph。"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from agent_runtime.api.schemas.chat import (
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
from agent_runtime.core.model import build_chat_model
from agent_runtime.graph.config import parent_thread_config
from agent_runtime.graph.parent import build_parent_graph
from agent_runtime.graph.router import StageOneRouter
from agent_runtime.persistence.checkpointers import open_stage_one_checkpointers
from agent_runtime.persistence.parent_state import PostgresParentStateStore
from agent_runtime.runs import (
    ActiveRun,
    ActiveRunRegistry,
    CancelReason,
    ProductRunEvent,
)
from agent_runtime.sessions.models import Session, SessionPage
from agent_runtime.sessions.repository import PostgresSessionRepository
from agent_runtime.sessions.service import SessionService

type CompletionStatus = Literal["completed", "unsupported"]


class ChatSessionError(ApplicationError):
    """Session 所属关系或 Parent 持久化状态不合法。"""


class ChatRuntimeError(ApplicationError):
    """Parent Graph 流式事件或完成状态不符合 Stage 1 契约。"""


@dataclass(frozen=True, slots=True)
class PreparedChatTurn:
    """在建立 SSE 前完成校验并分配稳定标识的一轮聊天。"""

    session_id: UUID
    human_message: HumanMessage
    response_message_id: UUID
    config: RunnableConfig
    active_run: ActiveRun


class ChatService:
    """查询 Session、准备聊天请求并维护 Parent Graph 公共消息。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_repository: PostgresSessionRepository,
        session_service: SessionService,
        parent_graph: Any,
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
        self._run_registry = run_registry or ActiveRunRegistry()
        self._session_id_factory = session_id_factory
        self._human_message_id_factory = human_message_id_factory
        self._response_message_id_factory = response_message_id_factory

    async def prepare_turn(
        self,
        *,
        session_id: UUID | None,
        content: str,
    ) -> PreparedChatTurn:
        """在建立 SSE 前创建或校验 Session，并分配本轮稳定消息 UUID。"""

        response_message_id = self._response_message_id_factory()
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

            return PreparedChatTurn(
                session_id=reserved_session_id,
                human_message=human_message,
                response_message_id=response_message_id,
                config=parent_thread_config(
                    reserved_session_id,
                    message_id=response_message_id,
                ),
                active_run=active_run,
            )
        except BaseException:
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

    async def stream_turn(
        self,
        turn: PreparedChatTurn,
    ) -> AsyncIterator[BaseMessage]:
        """只转发 Parent custom stream 中的公共消息事件。"""

        async for event in self._parent_graph.astream(
            {"messages": [turn.human_message]},
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

        task = asyncio.create_task(
            self._produce_turn(turn),
            name=f"chat-producer:{turn.session_id}",
        )
        try:
            await self._run_registry.attach_producer(turn.active_run, task)
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _produce_turn(self, turn: PreparedChatTurn) -> None:
        """执行 Parent Graph，并把产品事件写入本轮专用队列。"""

        partial_text: list[str] = []
        terminal_events: list[ProductRunEvent] = []
        cancelled = False
        try:
            try:
                async for message in self.stream_turn(turn):
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
                                delta=delta,
                            ),
                        )
                    )
                completion_status = await self.get_completion_status(turn)
                terminal_events.append(
                    ProductRunEvent(
                        name="done",
                        data=DoneEventData(
                            session_id=turn.session_id,
                            status=completion_status,
                        ),
                    )
                )
            except Exception as error:
                reported_error: Exception = error
                if partial_text:
                    try:
                        await self.persist_incomplete(turn, "".join(partial_text))
                    except Exception:
                        reported_error = ApplicationError(
                            code="CHAT_INCOMPLETE_PERSIST_FAILED",
                            message="未能保存模型的部分输出",
                            retryable=False,
                        )
                terminal_events = self._failed_terminal_events(
                    session_id=turn.session_id,
                    error=reported_error,
                )
        except asyncio.CancelledError:
            cancelled = True

        cancelled = await self._finalize_run(
            turn=turn,
            terminal_events=terminal_events,
            cancelled=cancelled,
        )

        if cancelled:
            raise asyncio.CancelledError

    async def _finalize_run(
        self,
        *,
        turn: PreparedChatTurn,
        terminal_events: list[ProductRunEvent],
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
                    terminal_events = self._failed_terminal_events(
                        session_id=turn.session_id,
                        error=ApplicationError(
                            code="CHAT_SESSION_TOUCH_FAILED",
                            message="未能更新 Session 的活跃时间",
                            retryable=False,
                        ),
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

    @staticmethod
    def _failed_terminal_events(
        *,
        session_id: UUID,
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

        return await self._run_registry.request_cancel(
            run,
            reason,
            unstarted_finalizer=lambda: self._finalize_unstarted_run(run),
        )

    async def _finalize_unstarted_run(self, run: ActiveRun) -> None:
        """为尚未启动 producer 的断开请求刷新 Session 并释放占用。"""

        try:
            try:
                await self._session_service.touch_session(
                    session_id=run.session_id
                )
            except Exception:
                pass
        finally:
            cleanup_cancelled = await self._await_run_cleanup(run)
            if cleanup_cancelled:
                raise asyncio.CancelledError

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

        parent_state = await self._parent_graph.aget_state(turn.config)
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
    ) -> None:
        """把已发送的部分文本作为 incomplete AIMessage 写回 Parent。"""

        message = AIMessage(
            content=content,
            id=str(turn.response_message_id),
            additional_kwargs={"runtime_status": "incomplete"},
        )
        await self._parent_graph.aupdate_state(
            turn.config,
            {"messages": [message], "completion_status": None},
            as_node="invoke_capability",
        )


@asynccontextmanager
async def open_chat_service(
    settings: Settings,
    *,
    model: BaseChatModel | None = None,
) -> AsyncIterator[ChatService]:
    """打开 Stage 1 数据库资源，并组装固定 Parent/Child 运行链路。"""

    session_repository = PostgresSessionRepository(settings)
    await session_repository.setup()
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
        service = ChatService(
            settings=settings,
            session_repository=session_repository,
            session_service=session_service,
            parent_graph=parent_graph,
        )
        try:
            yield service
        finally:
            await service.close()
