"""持久 Run 公开事件的 SSE 游标、编码与慢客户端写入保护。"""

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator
from uuid import UUID

from starlette.responses import StreamingResponse
from starlette.types import Send

from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event
from agent_runtime.runtime.event_gateway import GatewayHeartbeat, GatewayItem

logger = logging.getLogger(__name__)


class RunEventCursorError(ApplicationError):
    """客户端提供的 Run 事件续传游标不合法。"""


def resolve_run_event_cursor(
    *,
    last_event_id: str | None,
    after_seq: int,
) -> int:
    """优先解析 Last-Event-ID，否则使用非负 after_seq。"""

    if after_seq < 0:
        raise _cursor_error()
    if last_event_id is None:
        return after_seq
    try:
        normalized = last_event_id.strip()
        if not normalized:
            raise ValueError
        cursor = int(normalized)
    except ValueError as error:
        raise _cursor_error() from error
    if cursor < 0 or str(cursor) != normalized:
        raise _cursor_error()
    return cursor


def encode_run_event_sse(item: GatewayItem) -> str:
    """把公开事件编码为 id/event/data 帧，心跳编码为注释帧。"""

    if isinstance(item, GatewayHeartbeat):
        return ": heartbeat\n\n"
    return (
        f"id: {item.seq}\n"
        f"event: {item.event_type}\n"
        f"data: {item.model_dump_json()}\n\n"
    )


async def encode_run_event_stream(
    events: AsyncIterable[GatewayItem],
) -> AsyncIterator[str]:
    """逐项编码 Gateway 输出，不建立额外内存队列。"""

    async for item in events:
        yield encode_run_event_sse(item)


class RunEventStreamingResponse(StreamingResponse):
    """对每次响应体写入设置上限，慢客户端只关闭当前 SSE。"""

    def __init__(
        self,
        content: AsyncIterable[str | bytes],
        *,
        run_id: UUID | None = None,
        write_timeout_seconds: float = 5.0,
    ) -> None:
        if write_timeout_seconds <= 0:
            raise ValueError("SSE 写入超时时间必须大于 0 秒")
        self._run_id = run_id
        self._write_timeout_seconds = write_timeout_seconds
        super().__init__(
            content,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    async def stream_response(self, send: Send) -> None:
        """按 Starlette 流式协议发送，并在单次 body 写入超时时结束。"""

        try:
            try:
                await asyncio.wait_for(
                    send(
                        {
                            "type": "http.response.start",
                            "status": self.status_code,
                            "headers": self.raw_headers,
                        }
                    ),
                    timeout=self._write_timeout_seconds,
                )
            except TimeoutError:
                log_business_event(
                    logger,
                    "SSE网关响应头写入超时",
                    level=logging.WARNING,
                    run_id=self._run_id,
                    error_code="RUN_EVENT_WRITE_TIMEOUT",
                )
                return
            async for chunk in self.body_iterator:
                if not isinstance(chunk, (bytes, memoryview)):
                    chunk = chunk.encode(self.charset)
                elif isinstance(chunk, memoryview):
                    chunk = bytes(chunk)
                try:
                    await asyncio.wait_for(
                        send(
                            {
                                "type": "http.response.body",
                                "body": chunk,
                                "more_body": True,
                            }
                        ),
                        timeout=self._write_timeout_seconds,
                    )
                except TimeoutError:
                    log_business_event(
                        logger,
                        "SSE网关写入超时关闭",
                        level=logging.WARNING,
                        run_id=self._run_id,
                        error_code="RUN_EVENT_WRITE_TIMEOUT",
                    )
                    return
            try:
                await asyncio.wait_for(
                    send(
                        {
                            "type": "http.response.body",
                            "body": b"",
                            "more_body": False,
                        }
                    ),
                    timeout=self._write_timeout_seconds,
                )
            except TimeoutError:
                log_business_event(
                    logger,
                    "SSE网关结束帧写入超时",
                    level=logging.WARNING,
                    run_id=self._run_id,
                    error_code="RUN_EVENT_WRITE_TIMEOUT",
                )
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                await close()


def _cursor_error() -> RunEventCursorError:
    """构造不回显原始 Header 内容的稳定游标错误。"""

    return RunEventCursorError(
        code="RUN_EVENT_CURSOR_INVALID",
        message="Run 事件续传游标必须是非负整数",
        status_code=400,
    )
