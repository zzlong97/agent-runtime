"""合并 PostgreSQL durable event 与 Redis 实时事件的单连接 Gateway。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from time import monotonic, perf_counter
from typing import Protocol
from uuid import UUID

from agent_runtime.core.logging import log_business_event
from agent_runtime.runtime.event_models import RuntimeEvent
from agent_runtime.runtime.event_schemas import (
    PublicRuntimeEvent,
    project_public_event,
)
from agent_runtime.runtime.models import Run, TERMINAL_RUN_STATUSES

logger = logging.getLogger(__name__)


class _RunReader(Protocol):
    async def get(self, run_id: UUID) -> Run: ...


class _DurableEventReader(Protocol):
    async def list_public(
        self,
        *,
        run_id: UUID,
        after_seq: int,
        limit: int,
    ) -> list[RuntimeEvent]: ...


class _RealtimeEventReader(Protocol):
    async def read_public(
        self,
        *,
        run_id: UUID,
        after_seq: int,
        limit: int,
    ) -> list[PublicRuntimeEvent]: ...


@dataclass(frozen=True, slots=True)
class GatewayHeartbeat:
    """不分配 seq、不持久化的 SSE 注释心跳标记。"""


type GatewayItem = PublicRuntimeEvent | GatewayHeartbeat


class RuntimeEventGateway:
    """按 seq 有限批次合并两类公开事件，并在终态或中断后关闭。"""

    def __init__(
        self,
        *,
        run_repository: _RunReader,
        event_repository: _DurableEventReader,
        redis_reader: _RealtimeEventReader,
        batch_size: int = 100,
        poll_interval_seconds: float = 0.25,
        heartbeat_interval_seconds: float = 20.0,
    ) -> None:
        if batch_size < 1 or batch_size > 1000:
            raise ValueError("SSE Gateway 批次大小只允许 1 到 1000")
        if poll_interval_seconds <= 0:
            raise ValueError("SSE Gateway 轮询间隔必须大于 0 秒")
        if heartbeat_interval_seconds <= 0:
            raise ValueError("SSE Gateway 心跳间隔必须大于 0 秒")
        self._run_repository = run_repository
        self._event_repository = event_repository
        self._redis_reader = redis_reader
        self._batch_size = batch_size
        self._poll_interval_seconds = poll_interval_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds

    async def stream(
        self,
        *,
        run_id: UUID,
        after_seq: int,
    ) -> AsyncIterator[GatewayItem]:
        """只发送游标后的递增事件；合法缺号不会阻塞输出。"""

        if after_seq < 0:
            raise ValueError("SSE Gateway after_seq 不得小于 0")
        started_at = perf_counter()
        last_emitted_seq = after_seq
        last_output_at = monotonic()
        emitted_count = 0
        log_business_event(
            logger,
            "SSE网关合并开始",
            run_id=run_id,
            after_seq=after_seq,
            batch_size=self._batch_size,
        )
        try:
            while True:
                batch = await self._read_merged_batch(
                    run_id=run_id,
                    after_seq=last_emitted_seq,
                )
                if batch:
                    for event in batch:
                        if event.seq <= last_emitted_seq:
                            continue
                        yield event
                        last_emitted_seq = event.seq
                        emitted_count += 1
                        last_output_at = monotonic()
                    continue

                run = await self._run_repository.get(run_id)
                if run.status in TERMINAL_RUN_STATUSES or run.status == "interrupted":
                    # 首轮合并可能早于状态事务提交；关闭前用同一有界收口
                    # 再读两种存储，避免漏发终态前已经可见的较低序号事件。
                    terminal_batch = await self._read_merged_batch(
                        run_id=run_id,
                        after_seq=last_emitted_seq,
                    )
                    if terminal_batch:
                        for event in terminal_batch:
                            yield event
                            last_emitted_seq = event.seq
                            emitted_count += 1
                            last_output_at = monotonic()
                        continue
                    return

                now = monotonic()
                heartbeat_remaining = (
                    self._heartbeat_interval_seconds - (now - last_output_at)
                )
                if heartbeat_remaining <= 0:
                    yield GatewayHeartbeat()
                    last_output_at = monotonic()
                    continue
                await asyncio.sleep(
                    min(self._poll_interval_seconds, heartbeat_remaining)
                )
        finally:
            log_business_event(
                logger,
                "SSE网关合并结束",
                run_id=run_id,
                last_seq=last_emitted_seq,
                emitted_count=emitted_count,
                duration_ms=round((perf_counter() - started_at) * 1000, 2),
            )

    async def _read_merged_batch(
        self,
        *,
        run_id: UUID,
        after_seq: int,
    ) -> list[PublicRuntimeEvent]:
        """构造有界稳定批次，发送前收口跨存储读取窗口。"""

        realtime_events = await self._redis_reader.read_public(
            run_id=run_id,
            after_seq=after_seq,
            limit=self._batch_size,
        )
        durable_events = await self._event_repository.list_public(
            run_id=run_id,
            after_seq=after_seq,
            limit=self._batch_size,
        )
        projected_durable = [
            project_public_event(event) for event in durable_events
        ]
        realtime_frontier = max(
            (event.seq for event in realtime_events),
            default=after_seq,
        )
        durable_frontier = max(
            (event.seq for event in projected_durable),
            default=after_seq,
        )

        # PG 查询若把本轮可见前沿推进到 Redis 快照之后，较低 transient
        # 可能刚好发布在两次查询之间。只再读取一次 Redis，并仅接纳不高于
        # durable 前沿的事件；更高事件留到下一批，避免形成无限稳定化循环。
        if durable_frontier > realtime_frontier:
            closing_realtime = await self._redis_reader.read_public(
                run_id=run_id,
                after_seq=after_seq,
                limit=self._batch_size,
            )
            realtime_events = [
                *realtime_events,
                *(
                    event
                    for event in closing_realtime
                    if event.seq <= durable_frontier
                ),
            ]

        merged_by_seq = {
            event.seq: event
            for event in realtime_events
            if event.seq > after_seq
        }
        for event in projected_durable:
            if event.seq > after_seq:
                merged_by_seq[event.seq] = event
        return sorted(
            merged_by_seq.values(),
            key=lambda event: event.seq,
        )[: self._batch_size]
