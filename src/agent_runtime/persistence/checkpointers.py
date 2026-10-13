"""Parent 与动态 Child Capability 的 PostgreSQL Checkpointer 生命周期。"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent_runtime.core.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class RuntimeCheckpointers:
    """保存 Parent、清理入口与每个动态 Capability 的隔离 saver。"""

    parent: AsyncPostgresSaver
    cleanup: AsyncPostgresSaver
    children: tuple[AsyncPostgresSaver, ...]

    @property
    def general_chat(self) -> AsyncPostgresSaver:
        """兼容 S3-13 前仍按遗留线程名清理的普通聊天入口。"""

        return self.cleanup

    @property
    def en_to_zh(self) -> AsyncPostgresSaver:
        """兼容 S3-13 前仍按遗留线程名清理的英译汉入口。"""

        return self.cleanup


StageOneCheckpointers = RuntimeCheckpointers


@asynccontextmanager
async def open_runtime_checkpointers(
    settings: Settings | None = None,
    *,
    child_count: int = 0,
) -> AsyncIterator[RuntimeCheckpointers]:
    """打开 Parent、清理入口及指定数量的 Capability 隔离 saver。"""

    if type(child_count) is not int or child_count < 0:
        raise ValueError("Child Checkpointer 数量必须是非负整数")

    runtime_settings = settings or get_settings()
    connection_string = runtime_settings.database_connection_string
    async with AsyncExitStack() as stack:
        parent = await stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(connection_string)
        )
        cleanup = await stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(connection_string)
        )
        children = tuple(
            [
                await stack.enter_async_context(
                    AsyncPostgresSaver.from_conn_string(connection_string)
                )
                for _ in range(child_count)
            ]
        )
        for checkpointer in (parent, cleanup, *children):
            await checkpointer.setup()
        yield RuntimeCheckpointers(
            parent=parent,
            cleanup=cleanup,
            children=children,
        )


open_stage_one_checkpointers = open_runtime_checkpointers
