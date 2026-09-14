"""把当前活动 Parent 公共消息转换为分页产品历史。"""

from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.errors import ApplicationError
from agent_runtime.persistence.parent_state import PostgresParentStateStore
from agent_runtime.sessions.repository import PostgresSessionRepository

type ProductMessageRole = Literal["user", "assistant"]
type MessageRuntimeStatus = Literal[
    "completed",
    "unsupported",
    "incomplete",
    "stopped",
]
type MessageCapabilityId = Literal["general_chat", "en_to_zh"]
type MessageFeedback = Literal["like", "dislike"]

_RUNTIME_STATUSES = {
    "completed",
    "unsupported",
    "incomplete",
    "stopped",
}
_CAPABILITY_IDS = {"general_chat", "en_to_zh"}


class MessageHistoryError(ApplicationError):
    """历史查询参数或 Parent 公共消息不符合产品契约。"""


@dataclass(frozen=True, slots=True)
class ProductMessage:
    """只包含前端可见字段的公共消息。"""

    message_id: UUID
    role: ProductMessageRole
    content: str
    runtime_status: MessageRuntimeStatus | None
    capability_id: MessageCapabilityId | None
    feedback: MessageFeedback | None


@dataclass(frozen=True, slots=True)
class MessagePage:
    """当前活动 Parent 分支中按对话正序排列的一页消息。"""

    items: tuple[ProductMessage, ...]
    next_before: UUID | None


class MessageHistoryAdapter:
    """读取 Parent 权威消息，并在内存中完成过滤、转换与分页。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_repository: PostgresSessionRepository | None = None,
        parent_state_store: PostgresParentStateStore | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._session_repository = session_repository or PostgresSessionRepository(
            self._settings
        )
        self._parent_state_store = parent_state_store or PostgresParentStateStore(
            self._settings
        )

    async def list_messages(
        self,
        *,
        session_id: UUID,
        before: UUID | None,
        limit: int,
    ) -> MessagePage:
        """返回 before 之前的最近一页当前活动公共消息。"""

        if not 1 <= limit <= 100:
            raise MessageHistoryError(
                code="MESSAGE_LIMIT_INVALID",
                message="历史消息 limit 必须在 1 到 100 之间",
                status_code=400,
            )

        session = await self._session_repository.get(session_id)
        if session.user_id != self._settings.local_user_id:
            raise MessageHistoryError(
                code="SESSION_NOT_FOUND",
                message="Session 不存在",
                status_code=404,
            )

        parent_messages = await self._parent_state_store.get_messages(session_id)
        product_messages = tuple(
            product_message
            for message in parent_messages
            if (product_message := self._to_product_message(message)) is not None
        )

        end = len(product_messages)
        if before is not None:
            end = next(
                (
                    index
                    for index, message in enumerate(product_messages)
                    if message.message_id == before
                ),
                -1,
            )
            if end < 0:
                raise MessageHistoryError(
                    code="MESSAGE_BEFORE_INVALID",
                    message="before 不是当前活动历史中的消息",
                    status_code=400,
                )

        start = max(0, end - limit)
        items = product_messages[start:end]
        next_before = items[0].message_id if start > 0 else None
        return MessagePage(items=items, next_before=next_before)

    def _to_product_message(
        self,
        message: BaseMessage,
    ) -> ProductMessage | None:
        """过滤内部消息，并严格转换 HumanMessage 或 AIMessage。"""

        if not isinstance(message, (HumanMessage, AIMessage)):
            return None
        message_id = self._message_id(message)
        if isinstance(message, HumanMessage):
            return ProductMessage(
                message_id=message_id,
                role="user",
                content=str(message.text),
                runtime_status=None,
                capability_id=None,
                feedback=None,
            )

        runtime_status = message.additional_kwargs.get(
            "runtime_status",
            "completed",
        )
        if runtime_status not in _RUNTIME_STATUSES:
            raise MessageHistoryError(
                code="MESSAGE_HISTORY_INVALID",
                message="Parent 公共消息包含非法运行状态",
            )
        capability_id = message.additional_kwargs.get("capability_id")
        if capability_id is not None and capability_id not in _CAPABILITY_IDS:
            raise MessageHistoryError(
                code="MESSAGE_HISTORY_INVALID",
                message="Parent 公共消息包含非法能力标识",
            )
        return ProductMessage(
            message_id=message_id,
            role="assistant",
            content=str(message.text),
            runtime_status=cast(MessageRuntimeStatus, runtime_status),
            capability_id=cast(MessageCapabilityId | None, capability_id),
            feedback=None,
        )

    @staticmethod
    def _message_id(message: BaseMessage) -> UUID:
        """把持久化消息标识校验为产品层稳定 UUID。"""

        try:
            return UUID(str(message.id))
        except (TypeError, ValueError, AttributeError) as error:
            raise MessageHistoryError(
                code="MESSAGE_HISTORY_INVALID",
                message="Parent 公共消息缺少有效的 message_id",
            ) from error
