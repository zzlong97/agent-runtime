"""执行者Checkpoint写入与Run租约校验共用事务；普通服务入口尚未切换。"""

import asyncio
import logging
from collections.abc import Sequence
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions, Checkpoint, CheckpointMetadata,
    get_serializable_checkpoint_metadata,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from psycopg.types.json import Jsonb, JsonbDumper

from agent_runtime.core.logging import log_business_event
from agent_runtime.runtime.ownership import (
    PostgresRunOwnershipRepository,
    require_execution_token,
)

logger = logging.getLogger(__name__)


def _prepared_json(value: Any) -> Jsonb:
    """先完成JSON编码；驱动实际执行时只取已编码字节，避免惰性序列化占用行锁。"""
    encoded = JsonbDumper(Jsonb).dump(Jsonb(value))
    return Jsonb(None, dumps=lambda _: encoded)


class GuardedAsyncPostgresSaver(AsyncPostgresSaver):
    """保留框架读取与thread隔离，实际写入使用独享的受保护连接。"""

    def __init__(self, conn, *, ownership: PostgresRunOwnershipRepository, serde=None):
        super().__init__(conn, serde=serde)
        self._ownership = ownership

    def _prepare_checkpoint(
        self, config: RunnableConfig, checkpoint: Checkpoint,
        metadata: CheckpointMetadata, new_versions: ChannelVersions,
    ) -> tuple[RunnableConfig, list, tuple]:
        """对应锁定Saver3.1.2的写入表示，先准备blob、JSON和全部SQL参数。"""
        configurable = config["configurable"].copy()
        thread_id = configurable.pop("thread_id")
        checkpoint_ns = configurable.pop("checkpoint_ns")
        checkpoint_id = configurable.pop("checkpoint_id", None)
        copy = checkpoint.copy()
        copy["channel_values"] = copy["channel_values"].copy()
        blob_values = {}
        for key, value in checkpoint["channel_values"].items():
            if isinstance(value, _DeltaSnapshot):
                blob_values[key] = copy["channel_values"].pop(key)
                copy["channel_values"][key] = True
            elif value is None or isinstance(value, (str, int, float, bool)):
                continue
            else:
                blob_values[key] = copy["channel_values"].pop(key)
        blob_versions = {key: value for key, value in new_versions.items() if key in blob_values}
        blobs = self._dump_blobs(thread_id, checkpoint_ns, blob_values, blob_versions)
        params = (
            thread_id, checkpoint_ns, checkpoint["id"], checkpoint_id,
            _prepared_json(copy),
            _prepared_json(get_serializable_checkpoint_metadata(config, metadata)),
        )
        next_config = {"configurable": {
            "thread_id": thread_id, "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint["id"],
        }}
        return next_config, blobs, params

    async def aput(
        self, config: RunnableConfig, checkpoint: Checkpoint,
        metadata: CheckpointMetadata, new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """全部准备工作在短事务外完成，再由guard锁后重新校验执行权并实际写入。"""
        token = require_execution_token()
        started = perf_counter()
        try:
            next_config, blobs, params = await asyncio.to_thread(
                self._prepare_checkpoint, config, checkpoint, metadata, new_versions,
            )
        except BaseException as exc:
            log_business_event(
                logger, "检查点参数准备失败", run_id=token.run_id,
                error_type=type(exc).__name__, error_code="CHECKPOINT_PREPARATION_FAILED",
                duration_ms=round((perf_counter()-started)*1000, 3),
            )
            raise
        async with self._cursor(pipeline=True) as cursor:
            if blobs:
                await cursor.executemany(self.UPSERT_CHECKPOINT_BLOBS_SQL, blobs)
            await cursor.execute(self.UPSERT_CHECKPOINTS_SQL, params)
        return next_config

    async def aput_writes(
        self, config: RunnableConfig, writes: Sequence[tuple[str, Any]],
        task_id: str, task_path: str = "",
    ) -> None:
        """缺令牌先拒绝；框架3.1.2本身在cursor外完成writes序列化与参数准备。"""
        require_execution_token()
        await super().aput_writes(config, writes, task_id, task_path)

    @asynccontextmanager
    async def _cursor(self, *, pipeline=False):
        if not pipeline:
            # setup/只读查询沿用明确的初始化与读取连接，不借用执行写入入口。
            async with super()._cursor() as cursor:
                yield cursor
            return
        token = require_execution_token()
        # 排队等待进程内saver锁放在持锁事务外，避免占用Run租约事务预算。
        async with self.lock:
            async with self._ownership.guard(token) as conn:
                local = AsyncPostgresSaver(conn, serde=self.serde)
                # 框架pipeline退出和sync发生在guard事务提交之前；错误使整笔回滚。
                async with local._cursor(pipeline=True) as cursor:
                    yield cursor
