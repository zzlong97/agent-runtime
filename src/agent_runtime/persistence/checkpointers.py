"""Stage 1 Parent 与两个 Child 的 PostgreSQL Checkpointer 生命周期。"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent_runtime.core.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class StageOneCheckpointers:
    """保存三套同时存活且逻辑独立的 PostgreSQL saver。"""

    parent: AsyncPostgresSaver
    general_chat: AsyncPostgresSaver
    en_to_zh: AsyncPostgresSaver


@asynccontextmanager
async def open_stage_one_checkpointers(
    settings: Settings | None = None,
) -> AsyncIterator[StageOneCheckpointers]:
    """打开并初始化 Parent、普通聊天和英译汉三套独立 saver。"""

    runtime_settings = settings or get_settings()
    connection_string = runtime_settings.database_connection_string
    async with AsyncExitStack() as stack:
        parent = await stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(connection_string)
        )
        general_chat = await stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(connection_string)
        )
        en_to_zh = await stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(connection_string)
        )
        for checkpointer in (parent, general_chat, en_to_zh):
            await checkpointer.setup()
        yield StageOneCheckpointers(
            parent=parent,
            general_chat=general_chat,
            en_to_zh=en_to_zh,
        )
