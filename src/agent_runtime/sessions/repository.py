"""PostgreSQL persistence for Stage 1 Session metadata."""

from uuid import UUID

from agent_runtime.core.config import Settings, get_settings
from agent_runtime.persistence.database import open_database_connection
from agent_runtime.sessions.models import Session

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

_SELECT_SESSION = """
SELECT session_id, user_id, title, created_at, updated_at
FROM sessions
WHERE session_id = %s
"""


class PostgresSessionRepository:
    """Persist and restore the five-field Stage 1 Session entity."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def setup(self) -> None:
        """Create the Session metadata table when it does not exist."""

        async with open_database_connection(self._settings) as connection:
            await connection.execute(_CREATE_SESSIONS_TABLE)
            await connection.commit()

    async def add(self, session: Session) -> None:
        """Persist one Session before graph execution starts."""

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
        """Restore one existing Session by its stable UUID."""

        async with open_database_connection(self._settings) as connection:
            cursor = await connection.execute(_SELECT_SESSION, (session_id,))
            row = await cursor.fetchone()
        return Session(**row)
