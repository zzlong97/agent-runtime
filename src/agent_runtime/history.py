"""把当前活动 Parent 公共消息转换为分页产品历史。"""

from dataclasses import dataclass, replace
from typing import Literal, cast
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from agent_runtime.capabilities.manifest import (
    ManifestCapabilityId,
    validate_capability_id,
)
from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.errors import ApplicationError
from agent_runtime.feedback import PostgresFeedbackStore
from agent_runtime.persistence.parent_state import (
    ParentStateNotFoundError,
    PostgresParentStateStore,
)
from agent_runtime.sessions.repository import PostgresSessionRepository

type ProductMessageRole = Literal["user", "assistant"]
type MessageRuntimeStatus = Literal[
    "completed",
    "unsupported",
    "incomplete",
    "stopped",
]
type MessageCapabilityId = ManifestCapabilityId
type MessageFeedback = Literal["like", "dislike"]

_RUNTIME_STATUSES = {
    "completed",
    "unsupported",
    "incomplete",
    "stopped",
}


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
        feedback_store: PostgresFeedbackStore | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._session_repository = session_repository or PostgresSessionRepository(
            self._settings
        )
        self._parent_state_store = parent_state_store or PostgresParentStateStore(
            self._settings
        )
        self._feedback_store = feedback_store

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

        product_messages = await self.get_active_messages(session_id=session_id)

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

    async def get_active_messages(
        self,
        *,
        session_id: UUID,
    ) -> tuple[ProductMessage, ...]:
        """返回当前活动 Parent checkpoint 的完整产品消息。"""

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
        if self._feedback_store is None:
            return product_messages

        completed_message_ids = tuple(
            message.message_id
            for message in product_messages
            if message.role == "assistant"
            and message.runtime_status == "completed"
        )
        feedback_by_message = await self._feedback_store.list_for_messages(
            user_id=self._settings.local_user_id,
            message_ids=completed_message_ids,
        )
        return tuple(
            replace(
                message,
                feedback=feedback_by_message.get(message.message_id),
            )
            if message.message_id in feedback_by_message
            else message
            for message in product_messages
        )

    async def find_feedback_session(self, *, message_id: UUID) -> UUID:
        """在固定用户所有当前活动 Parent 分支中定位可反馈回答。"""

        session_ids = await self._session_repository.list_ids_by_user(
            user_id=self._settings.local_user_id
        )
        for session_id in session_ids:
            try:
                parent_messages = await self._parent_state_store.get_messages(
                    session_id
                )
            except ParentStateNotFoundError:
                continue
            for message in parent_messages:
                if str(message.id) != str(message_id):
                    continue
                product_message = self._to_product_message(message)
                if (
                    product_message is not None
                    and product_message.role == "assistant"
                    and product_message.runtime_status == "completed"
                ):
                    return session_id
                raise self._feedback_not_allowed()
        raise self._feedback_not_allowed()

    @staticmethod
    def _feedback_not_allowed() -> MessageHistoryError:
        """返回不泄露消息归属与历史分支信息的统一反馈错误。"""

        return MessageHistoryError(
            code="MESSAGE_FEEDBACK_NOT_ALLOWED",
            message="仅允许反馈当前活动分支中的 completed AIMessage",
            status_code=409,
        )

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
        if capability_id is not None:
            try:
                capability_id = validate_capability_id(capability_id)
            except (TypeError, ValueError) as error:
                raise MessageHistoryError(
                    code="MESSAGE_HISTORY_INVALID",
                    message="Parent 公共消息包含非法能力标识",
                ) from error
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
