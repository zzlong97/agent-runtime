"""Redis Stream 公开实时事件发布与故障降级。"""

import logging
from time import perf_counter
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from agent_runtime.core.logging import log_business_event
from agent_runtime.runtime.event_models import RuntimeEvent
from agent_runtime.runtime.event_schemas import (
    PublicRuntimeEvent,
    PublicEventProjectionError,
    parse_public_event_json,
    project_public_event,
)

logger = logging.getLogger(__name__)

_STREAM_KEY_PREFIX = "runtime:events:"


class RedisStreamPublisher:
    """将固定公开事件尽力写入短期 Redis Stream。"""

    def __init__(self, *, client: Any, ttl_seconds: int = 1800) -> None:
        if ttl_seconds < 1:
            raise ValueError("Redis Stream TTL 必须大于等于 1 秒")
        self._client = client
        self._ttl_seconds = ttl_seconds

    @classmethod
    def from_url(
        cls,
        redis_url: str,
        *,
        ttl_seconds: int = 1800,
        socket_timeout_seconds: float = 0.5,
    ) -> "RedisStreamPublisher":
        """创建延迟建连客户端，使 Redis 启动失败时仍可构造 Runtime。"""

        if socket_timeout_seconds <= 0:
            raise ValueError("Redis 超时时间必须大于 0 秒")
        client = Redis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=socket_timeout_seconds,
            socket_timeout=socket_timeout_seconds,
            retry_on_timeout=False,
        )
        return cls(client=client, ttl_seconds=ttl_seconds)

    async def publish(self, event: RuntimeEvent) -> bool:
        """发布一个公开事件；Redis 故障只返回 False，不影响 Run 主流程。"""

        try:
            public_event = project_public_event(event)
        except PublicEventProjectionError as error:
            if event.event_type != "message.delta":
                log_business_event(
                    logger,
                    "Redis实时事件发布拒绝",
                    level=logging.WARNING,
                    run_id=event.run_id,
                    event_id=event.event_id,
                    event_type=event.event_type,
                    seq=event.seq,
                    error_code=error.code,
                )
            return False

        stream_key = f"{_STREAM_KEY_PREFIX}{event.run_id}"
        stream_id = f"{event.seq}-0"
        started_at = perf_counter()
        if event.event_type != "message.delta":
            log_business_event(
                logger,
                "Redis实时事件发布开始",
                run_id=event.run_id,
                event_id=event.event_id,
                event_type=event.event_type,
                seq=event.seq,
            )
        try:
            pipeline = self._client.pipeline(transaction=True)
            pipeline.xadd(
                stream_key,
                {"event": public_event.model_dump_json()},
                id=stream_id,
            )
            pipeline.expire(stream_key, self._ttl_seconds)
            await pipeline.execute()
        except Exception as error:
            if event.event_type != "message.delta":
                log_business_event(
                    logger,
                    "Redis实时事件发布降级",
                    level=logging.WARNING,
                    run_id=event.run_id,
                    event_id=event.event_id,
                    event_type=event.event_type,
                    seq=event.seq,
                    error_code="REDIS_EVENT_PUBLISH_FAILED",
                    error_type=type(error).__name__,
                    duration_ms=_elapsed_ms(started_at),
                )
            return False
        if event.event_type != "message.delta":
            log_business_event(
                logger,
                "Redis实时事件发布完成",
                run_id=event.run_id,
                event_id=event.event_id,
                event_type=event.event_type,
                seq=event.seq,
                duration_ms=_elapsed_ms(started_at),
            )
        return True

    async def delete_streams(self, run_ids: list[UUID]) -> bool:
        """尽力删除一组 Run Stream；Redis 故障不得阻塞 Session 硬删除。"""

        if not run_ids:
            return True
        stream_keys = [f"{_STREAM_KEY_PREFIX}{run_id}" for run_id in run_ids]
        try:
            await self._client.delete(*stream_keys)
        except Exception as error:
            log_business_event(
                logger,
                "Redis实时事件清理降级",
                level=logging.WARNING,
                run_count=len(run_ids),
                error_code="REDIS_EVENT_DELETE_FAILED",
                error_type=type(error).__name__,
            )
            return False
        log_business_event(
            logger,
            "Redis实时事件清理完成",
            run_count=len(run_ids),
        )
        return True

    async def aclose(self) -> None:
        """关闭 Redis 连接池；关闭失败按旁路故障静默降级。"""

        try:
            await self._client.aclose()
        except Exception as error:
            log_business_event(
                logger,
                "Redis实时事件连接关闭降级",
                level=logging.WARNING,
                error_code="REDIS_EVENT_CLOSE_FAILED",
                error_type=type(error).__name__,
            )


class RedisStreamReader:
    """按 Run 序号有限读取 Redis 中的短期公开实时事件。"""

    def __init__(self, *, client: Any) -> None:
        self._client = client

    @classmethod
    def from_url(
        cls,
        redis_url: str,
        *,
        socket_timeout_seconds: float = 0.5,
    ) -> "RedisStreamReader":
        """创建延迟建连读取客户端，Redis 不可用时由读取方法降级。"""

        if socket_timeout_seconds <= 0:
            raise ValueError("Redis 超时时间必须大于 0 秒")
        client = Redis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=socket_timeout_seconds,
            socket_timeout=socket_timeout_seconds,
            retry_on_timeout=False,
        )
        return cls(client=client)

    async def read_public(
        self,
        *,
        run_id: UUID,
        after_seq: int,
        limit: int,
    ) -> list[PublicRuntimeEvent]:
        """读取 seq 大于游标的公开事件；故障或坏数据只降级丢弃。"""

        if after_seq < 0:
            raise ValueError("Redis RuntimeEvent after_seq 不得小于 0")
        if limit < 1 or limit > 1000:
            raise ValueError("Redis RuntimeEvent 查询条数只允许 1 到 1000")
        stream_key = f"{_STREAM_KEY_PREFIX}{run_id}"
        try:
            streams = await self._client.xread(
                {stream_key: f"{after_seq}-0"},
                count=limit,
            )
        except Exception as error:
            log_business_event(
                logger,
                "Redis实时事件读取降级",
                level=logging.WARNING,
                run_id=run_id,
                error_code="REDIS_EVENT_READ_FAILED",
                error_type=type(error).__name__,
            )
            return []

        events: list[PublicRuntimeEvent] = []
        for _stream_name, entries in streams:
            for stream_id, fields in entries:
                try:
                    seq_text, separator, suffix = stream_id.partition("-")
                    if separator != "-" or suffix != "0":
                        raise ValueError("Redis Stream ID 必须使用固定 {seq}-0 格式")
                    seq = int(seq_text)
                    raw_event = fields.get("event")
                    if not isinstance(raw_event, (str, bytes)):
                        raise ValueError("Redis Stream 缺少公开事件字段")
                    event = parse_public_event_json(raw_event)
                    if (
                        event.run_id != run_id
                        or event.seq != seq
                        or event.seq <= after_seq
                    ):
                        raise ValueError("Redis Stream 事件关联或序号不一致")
                except (ValueError, PublicEventProjectionError) as error:
                    log_business_event(
                        logger,
                        "Redis实时事件读取拒绝",
                        level=logging.WARNING,
                        run_id=run_id,
                        error_code="REDIS_EVENT_SCHEMA_INVALID",
                        error_type=type(error).__name__,
                    )
                    continue
                events.append(event)
        return events

    async def aclose(self) -> None:
        """关闭 Redis 读取连接池；失败只记录旁路降级日志。"""

        try:
            await self._client.aclose()
        except Exception as error:
            log_business_event(
                logger,
                "Redis实时事件读取连接关闭降级",
                level=logging.WARNING,
                error_code="REDIS_EVENT_READ_CLOSE_FAILED",
                error_type=type(error).__name__,
            )


def _elapsed_ms(started_at: float) -> int:
    """返回用于业务日志的整数毫秒耗时。"""

    return round((perf_counter() - started_at) * 1000)
