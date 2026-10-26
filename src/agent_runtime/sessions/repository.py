"""Session 元数据的 PostgreSQL 持久化与产品查询。"""

import logging
from datetime import datetime
from time import perf_counter
from uuid import UUID

from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event
from agent_runtime.persistence.database import open_database_connection
from agent_runtime.sessions.models import Session, SessionCursor

logger = logging.getLogger(__name__)

_COMMENT_SESSIONS_TABLE = """
COMMENT ON TABLE sessions IS '保存 Session 业务元数据；每条记录标识一个固定用户的会话，并作为该会话业务数据与删除流程的根记录。'
"""

_CREATE_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
)
"""

_INSERT_SESSION = """
INSERT INTO sessions (session_id, user_id, title, created_at, updated_at)
VALUES (%s, %s, %s, %s, %s)
"""

_CREATE_SESSION_LIST_INDEX = """
CREATE INDEX IF NOT EXISTS sessions_user_updated_id_idx
ON sessions (user_id, updated_at DESC, session_id DESC)
"""

_SELECT_SESSION = """
SELECT session_id, user_id, title, created_at, updated_at
FROM sessions
WHERE session_id = %s
"""

_SELECT_SESSION_IDS_BY_USER = """
SELECT session_id
FROM sessions
WHERE user_id = %s
"""

_SELECT_SESSION_PAGE = """
SELECT session_id, user_id, title, created_at, updated_at
FROM sessions
WHERE user_id = %s
ORDER BY updated_at DESC, session_id DESC
LIMIT %s
"""

_SELECT_SESSION_PAGE_AFTER = """
SELECT session_id, user_id, title, created_at, updated_at
FROM sessions
WHERE user_id = %s
AND (updated_at, session_id) < (%s, %s)
ORDER BY updated_at DESC, session_id DESC
LIMIT %s
"""

_RENAME_SESSION = """
UPDATE sessions
SET title = %s, updated_at = %s
WHERE session_id = %s AND user_id = %s
RETURNING session_id, user_id, title, created_at, updated_at
"""

_TOUCH_SESSION = """
UPDATE sessions
SET updated_at = %s
WHERE session_id = %s AND user_id = %s
RETURNING session_id, user_id, title, created_at, updated_at
"""

_DELETE_OWNED_SESSION = """
DELETE FROM sessions
WHERE session_id = %s AND user_id = %s
"""


class SessionNotFoundError(ApplicationError):
    """客户端指定的 Session 不存在。"""


class PostgresSessionRepository:
    """持久化、恢复并分页查询五字段 Session 实体。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def setup(self) -> None:
        """幂等创建 Session 表和索引，并补充或修正表注释。"""

        started_at = perf_counter()
        log_business_event(logger, "Session持久化初始化开始")
        try:
            async with open_database_connection(self._settings) as connection:
                await connection.execute(_CREATE_SESSIONS_TABLE)
                await connection.execute(_CREATE_SESSION_LIST_INDEX)
                await connection.execute(_COMMENT_SESSIONS_TABLE)
                await connection.commit()
        except Exception as error:
            log_business_event(
                logger,
                "Session持久化初始化失败",
                level=logging.ERROR,
                error_code="SESSION_PERSISTENCE_SETUP_FAILED",
                error_type=type(error).__name__,
                duration_ms=round((perf_counter() - started_at) * 1000, 3),
            )
            raise
        log_business_event(
            logger,
            "Session持久化初始化完成",
            duration_ms=round((perf_counter() - started_at) * 1000, 3),
        )

    async def add(self, session: Session) -> None:
        """在图执行开始前持久化一个 Session。"""

        async with open_database_connection(self._settings) as connection:
            await connection.execute(
                _INSERT_SESSION,
                (
                    session.session_id,
                    session.user_id,
                    session.title,
                    session.created_at,
                    session.updated_at,
                ),
            )
            await connection.commit()

    async def get(self, session_id: UUID) -> Session:
        """按稳定 UUID 恢复 Session，不存在时返回标准 404 应用错误。"""

        async with open_database_connection(self._settings) as connection:
            cursor = await connection.execute(_SELECT_SESSION, (session_id,))
            row = await cursor.fetchone()
        if row is None:
            raise SessionNotFoundError(
                code="SESSION_NOT_FOUND",
                message="Session 不存在",
                status_code=404,
            )
        return Session(**row)

    async def list_ids_by_user(self, *, user_id: str) -> list[UUID]:
        """列出固定用户的 Session UUID，供跨活动分支定位消息使用。"""

        async with open_database_connection(self._settings) as connection:
            cursor = await connection.execute(
                _SELECT_SESSION_IDS_BY_USER,
                (user_id,),
            )
            rows = await cursor.fetchall()
        return [UUID(str(row["session_id"])) for row in rows]

    async def delete_owned(self, *, session_id: UUID, user_id: str) -> None:
        """幂等删除指定用户拥有的 Session 行。"""

        async with open_database_connection(self._settings) as connection:
            await connection.execute(
                _DELETE_OWNED_SESSION,
                (session_id, user_id),
            )
            await connection.commit()

    async def list_page(
        self,
        *,
        user_id: str,
        cursor: SessionCursor | None,
        limit: int,
    ) -> tuple[list[Session], bool]:
        """按固定用户和复合降序键查询一页，并多取一条判断后续页。"""

        fetch_limit = limit + 1
        if cursor is None:
            query = _SELECT_SESSION_PAGE
            params = (user_id, fetch_limit)
        else:
            query = _SELECT_SESSION_PAGE_AFTER
            params = (
                user_id,
                cursor.updated_at,
                cursor.session_id,
                fetch_limit,
            )

        async with open_database_connection(self._settings) as connection:
            result = await connection.execute(query, params)
            rows = await result.fetchall()

        sessions = [Session(**row) for row in rows]
        return sessions[:limit], len(sessions) > limit

    async def rename(
        self,
        *,
        session_id: UUID,
        user_id: str,
        title: str,
        updated_at: datetime,
    ) -> Session:
        """只更新指定用户拥有的 Session，并返回持久化后的完整元数据。"""

        async with open_database_connection(self._settings) as connection:
            cursor = await connection.execute(
                _RENAME_SESSION,
                (title, updated_at, session_id, user_id),
            )
            row = await cursor.fetchone()
            await connection.commit()
        if row is None:
            raise SessionNotFoundError(
                code="SESSION_NOT_FOUND",
                message="Session 不存在",
                status_code=404,
            )
        return Session(**row)

    async def touch(
        self,
        *,
        session_id: UUID,
        user_id: str,
        updated_at: datetime,
    ) -> Session:
        """刷新指定用户拥有的 Session 活跃时间，并返回完整元数据。"""

        async with open_database_connection(self._settings) as connection:
            cursor = await connection.execute(
                _TOUCH_SESSION,
                (updated_at, session_id, user_id),
            )
            row = await cursor.fetchone()
            await connection.commit()
        if row is None:
            raise SessionNotFoundError(
                code="SESSION_NOT_FOUND",
                message="Session 不存在",
                status_code=404,
            )
        return Session(**row)
