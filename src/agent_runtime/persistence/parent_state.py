"""Minimal Parent state persistence backed by LangGraph checkpoints."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent_runtime.core.config import Settings, get_settings


@asynccontextmanager
async def _open_checkpointer(
    settings: Settings,
) -> AsyncIterator[AsyncPostgresSaver]:
    async with AsyncPostgresSaver.from_conn_string(
        settings.database_connection_string
    ) as checkpointer:
        yield checkpointer


class PostgresParentStateStore:
    """Persist Parent public state without creating a second message store."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def setup(self) -> None:
        """Initialize LangGraph's PostgreSQL checkpoint tables."""

        async with _open_checkpointer(self._settings) as checkpointer:
            await checkpointer.setup()

    async def store_initial_human_message(
        self,
        session_id: UUID,
        message: HumanMessage,
    ) -> None:
        """Create the Parent checkpoint containing the first HumanMessage."""

        async with _open_checkpointer(self._settings) as checkpointer:
            version = checkpointer.get_next_version(None, None)
            checkpoint = empty_checkpoint()
            checkpoint["channel_values"]["messages"] = [message]
            checkpoint["channel_versions"]["messages"] = version
            checkpoint["updated_channels"] = ["messages"]
            await checkpointer.aput(
                {
                    "configurable": {
                        "thread_id": str(session_id),
                        "checkpoint_ns": "",
                    }
                },
                checkpoint,
                {
                    "source": "input",
                    "step": -1,
                    "parents": {},
                },
                {"messages": version},
            )

    async def get_messages(self, session_id: UUID) -> list[BaseMessage]:
        """Restore the Parent's authoritative public message list."""

        async with _open_checkpointer(self._settings) as checkpointer:
            checkpoint_tuple = await checkpointer.aget_tuple(
                {
                    "configurable": {
                        "thread_id": str(session_id),
                        "checkpoint_ns": "",
                    }
                }
            )
        return list(checkpoint_tuple.checkpoint["channel_values"]["messages"])
