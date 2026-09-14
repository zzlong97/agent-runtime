"""Stage 2 Session 持久化关联数据硬删除服务。"""

from typing import Protocol
from uuid import UUID

from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.errors import ApplicationError


class SessionDeletionError(ApplicationError):
    """Session 关联数据删除失败时返回的稳定应用错误。"""


class ThreadCheckpointer(Protocol):
    """删除单个 LangGraph thread 所需的最小接口。"""

    async def adelete_thread(self, thread_id: str) -> None:
        """幂等删除指定 thread 的 checkpoints、blobs 和 writes。"""


class SessionFeedbackDeleter(Protocol):
    """按 Session 清理固定用户反馈所需的最小接口。"""

    async def delete_by_session(
        self,
        *,
        user_id: str,
        session_id: UUID,
    ) -> None:
        """幂等删除指定 Session 的全部反馈。"""


class SessionRowDeleter(Protocol):
    """最后删除 Session 重试锚点所需的最小接口。"""

    async def delete_owned(
        self,
        *,
        session_id: UUID,
        user_id: str,
    ) -> None:
        """幂等删除固定用户拥有的 Session 行。"""


class SessionDeletionService:
    """按已确认顺序删除 Session 的全部持久化关联数据。"""

    def __init__(
        self,
        *,
        parent_checkpointer: ThreadCheckpointer,
        general_chat_checkpointer: ThreadCheckpointer,
        en_to_zh_checkpointer: ThreadCheckpointer,
        feedback_store: SessionFeedbackDeleter,
        session_repository: SessionRowDeleter,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._parent_checkpointer = parent_checkpointer
        self._general_chat_checkpointer = general_chat_checkpointer
        self._en_to_zh_checkpointer = en_to_zh_checkpointer
        self._feedback_store = feedback_store
        self._session_repository = session_repository

    async def delete_persisted_data(self, *, session_id: UUID) -> None:
        """先删 Child 和 Parent 状态，再删反馈，最后删除 Session 行。"""

        try:
            await self._general_chat_checkpointer.adelete_thread(
                f"{session_id}:general_chat"
            )
            await self._en_to_zh_checkpointer.adelete_thread(
                f"{session_id}:en_to_zh"
            )
            await self._parent_checkpointer.adelete_thread(str(session_id))
            await self._feedback_store.delete_by_session(
                user_id=self._settings.local_user_id,
                session_id=session_id,
            )
            await self._session_repository.delete_owned(
                session_id=session_id,
                user_id=self._settings.local_user_id,
            )
        except Exception as error:
            raise SessionDeletionError(
                code="SESSION_DELETE_FAILED",
                message="Session 关联数据删除失败，请重试",
                status_code=500,
                retryable=True,
            ) from error
