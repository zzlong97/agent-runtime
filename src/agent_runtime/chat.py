"""聊天应用层，连接 Session 产品能力与 Parent Graph。"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

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


class ChatService:
    """查询 Session、准备聊天请求并维护 Parent Graph 公共消息。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_repository: PostgresSessionRepository,
        session_service: SessionService,
        parent_graph: Any,
        human_message_id_factory: Callable[[], UUID] = uuid4,
        response_message_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        """绑定 Session 持久化、Parent Graph 和服务端 UUID 生成器。"""

        self._settings = settings or get_settings()
        self._session_repository = session_repository
        self._session_service = session_service
        self._parent_graph = parent_graph
        self._human_message_id_factory = human_message_id_factory
        self._response_message_id_factory = response_message_id_factory

    async def prepare_turn(
        self,
        *,
        session_id: UUID | None,
        content: str,
    ) -> PreparedChatTurn:
        """在建立 SSE 前创建或校验 Session，并分配本轮稳定消息 UUID。"""

        if session_id is None:
            started = await self._session_service.prepare_new_session(
                content=content
            )
            session_id = started.session.session_id
            human_message = started.human_message
        else:
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
            human_message = HumanMessage(
                content=content,
                id=str(self._human_message_id_factory()),
            )

        response_message_id = self._response_message_id_factory()
        return PreparedChatTurn(
            session_id=session_id,
            human_message=human_message,
            response_message_id=response_message_id,
            config=parent_thread_config(
                session_id,
                message_id=response_message_id,
            ),
        )

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
        yield ChatService(
            settings=settings,
            session_repository=session_repository,
            session_service=session_service,
            parent_graph=parent_graph,
        )
