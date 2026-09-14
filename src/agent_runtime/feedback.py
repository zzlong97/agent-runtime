"""Stage 2 消息反馈应用服务与 PostgreSQL 存储。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Literal, Protocol, cast
from uuid import UUID

from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.errors import ApplicationError
from agent_runtime.persistence.database import open_database_connection

FeedbackAction = Literal["like", "dislike", "cancel"]
FeedbackValue = Literal["like", "dislike"]

_FEEDBACK_VALUES = frozenset({"like", "dislike"})

_CREATE_FEEDBACK_TABLE = """
CREATE TABLE IF NOT EXISTS message_feedback (
    user_id TEXT NOT NULL,
    session_id UUID NOT NULL,
    message_id UUID NOT NULL,
    value TEXT NOT NULL CHECK (value IN ('like', 'dislike')),
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, message_id)
)
"""

_CREATE_FEEDBACK_SESSION_INDEX = """
CREATE INDEX IF NOT EXISTS message_feedback_session_id_idx
ON message_feedback (session_id)
"""

_UPSERT_FEEDBACK = """
INSERT INTO message_feedback
    (user_id, session_id, message_id, value, updated_at)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (user_id, message_id) DO UPDATE SET
    session_id = EXCLUDED.session_id,
    value = EXCLUDED.value,
    updated_at = EXCLUDED.updated_at
"""

_DELETE_FEEDBACK = """
DELETE FROM message_feedback
WHERE user_id = %s AND message_id = %s
"""

_SELECT_FEEDBACK_FOR_MESSAGES = """
SELECT message_id, value
FROM message_feedback
WHERE user_id = %s AND message_id = ANY(%s::uuid[])
"""


class FeedbackError(ApplicationError):
    """反馈读写失败时对产品层暴露的稳定应用错误。"""


class ActiveFeedbackTargetFinder(Protocol):
    """从当前活动 Parent 分支定位可反馈回答。"""

    async def find_feedback_session(self, *, message_id: UUID) -> UUID:
        """校验目标并返回目标所属 Session UUID。"""


class FeedbackStore(Protocol):
    """反馈服务所需的最小持久化接口。"""

    async def upsert(
        self,
        *,
        user_id: str,
        session_id: UUID,
        message_id: UUID,
        value: FeedbackValue,
        updated_at: datetime,
    ) -> None:
        """新增或替换同一用户对同一消息的最终反馈。"""

    async def delete(self, *, user_id: str, message_id: UUID) -> None:
        """幂等删除同一用户对同一消息的反馈。"""


@dataclass(frozen=True, slots=True)
class FeedbackResult:
    """反馈操作完成后的最终产品结果。"""

    message_id: UUID
    feedback: FeedbackValue | None


class PostgresFeedbackStore:
    """使用唯一键保存每个用户对每条消息的最终反馈。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def setup(self) -> None:
        """创建反馈表及供后续 Session 清理使用的索引。"""

        async with open_database_connection(self._settings) as connection:
            await connection.execute(_CREATE_FEEDBACK_TABLE)
            await connection.execute(_CREATE_FEEDBACK_SESSION_INDEX)
            await connection.commit()

    async def upsert(
        self,
        *,
        user_id: str,
        session_id: UUID,
        message_id: UUID,
        value: FeedbackValue,
        updated_at: datetime,
    ) -> None:
        """原子新增或覆盖一条最终反馈，不产生重复记录。"""

        try:
            async with open_database_connection(self._settings) as connection:
                await connection.execute(
                    _UPSERT_FEEDBACK,
                    (user_id, session_id, message_id, value, updated_at),
                )
                await connection.commit()
        except Exception as error:
            raise FeedbackError(
                code="MESSAGE_FEEDBACK_PERSIST_FAILED",
                message="消息反馈保存失败",
                retryable=True,
            ) from error

    async def delete(self, *, user_id: str, message_id: UUID) -> None:
        """幂等删除反馈；记录不存在时仍视为成功。"""

        try:
            async with open_database_connection(self._settings) as connection:
                await connection.execute(
                    _DELETE_FEEDBACK,
                    (user_id, message_id),
                )
                await connection.commit()
        except Exception as error:
            raise FeedbackError(
                code="MESSAGE_FEEDBACK_PERSIST_FAILED",
                message="消息反馈保存失败",
                retryable=True,
            ) from error

    async def list_for_messages(
        self,
        *,
        user_id: str,
        message_ids: tuple[UUID, ...],
    ) -> dict[UUID, FeedbackValue]:
        """一次读取指定当前分支消息的最终反馈。"""

        if not message_ids:
            return {}
        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _SELECT_FEEDBACK_FOR_MESSAGES,
                    (user_id, list(message_ids)),
                )
                rows = await cursor.fetchall()
            result: dict[UUID, FeedbackValue] = {}
            for row in rows:
                value = row["value"]
                if value not in _FEEDBACK_VALUES:
                    raise ValueError("数据库包含非法反馈值")
                result[UUID(str(row["message_id"]))] = cast(
                    FeedbackValue,
                    value,
                )
            return result
        except Exception as error:
            raise FeedbackError(
                code="MESSAGE_FEEDBACK_READ_FAILED",
                message="消息反馈读取失败",
                retryable=True,
            ) from error


class FeedbackService:
    """在验证当前活动分支资格后保存消息反馈。"""

    def __init__(
        self,
        *,
        target_finder: ActiveFeedbackTargetFinder,
        store: FeedbackStore,
        settings: Settings | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._target_finder = target_finder
        self._store = store
        self._now_factory = now_factory or (lambda: datetime.now(UTC))

    async def submit(
        self,
        *,
        message_id: UUID,
        action: FeedbackAction,
    ) -> FeedbackResult:
        """校验目标后保存、替换或取消最终反馈。"""

        session_id = await self._target_finder.find_feedback_session(
            message_id=message_id
        )
        if action == "cancel":
            await self._store.delete(
                user_id=self._settings.local_user_id,
                message_id=message_id,
            )
            return FeedbackResult(message_id=message_id, feedback=None)

        await self._store.upsert(
            user_id=self._settings.local_user_id,
            session_id=session_id,
            message_id=message_id,
            value=action,
            updated_at=self._now_factory(),
        )
        return FeedbackResult(message_id=message_id, feedback=action)
