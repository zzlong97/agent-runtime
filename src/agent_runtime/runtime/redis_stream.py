"""Redis Stream 公开实时事件发布与故障降级。"""

import logging
from time import perf_counter
from typing import Any

from redis.asyncio import Redis

from agent_runtime.core.logging import log_business_event
from agent_runtime.runtime.event_models import RuntimeEvent
from agent_runtime.runtime.event_schemas import (
    PublicEventProjectionError,
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


def _elapsed_ms(started_at: float) -> int:
    """返回用于业务日志的整数毫秒耗时。"""

    return round((perf_counter() - started_at) * 1000)
