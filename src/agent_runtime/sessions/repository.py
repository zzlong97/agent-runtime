"""Session 元数据的 PostgreSQL 持久化与产品查询。"""

from datetime import datetime
from uuid import UUID

from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.errors import ApplicationError
from agent_runtime.persistence.database import open_database_connection
from agent_runtime.sessions.models import Session, SessionCursor

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


class SessionNotFoundError(ApplicationError):
    """客户端指定的 Session 不存在。"""


class PostgresSessionRepository:
    """持久化、恢复并分页查询五字段 Session 实体。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def setup(self) -> None:
        """在 Session 元数据表不存在时创建该表。"""

        async with open_database_connection(self._settings) as connection:
            await connection.execute(_CREATE_SESSIONS_TABLE)
            await connection.execute(_CREATE_SESSION_LIST_INDEX)
            await connection.commit()

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
