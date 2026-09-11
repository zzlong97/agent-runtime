"""PostgreSQL connection lifecycle."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from agent_runtime.core.config import Settings, get_settings


@asynccontextmanager
async def open_database_connection(
    settings: Settings | None = None,
) -> AsyncIterator[AsyncConnection[dict[str, Any]]]:
    """Open and reliably close one asynchronous PostgreSQL connection."""

    runtime_settings = settings or get_settings()
    connection = await AsyncConnection.connect(
        runtime_settings.database_connection_string,
        row_factory=dict_row,
    )
    try:
        yield connection
    finally:
        await connection.close()
