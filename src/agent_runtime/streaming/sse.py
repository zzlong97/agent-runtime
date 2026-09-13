"""将独立 producer 的产品事件队列编码为 SSE。"""

from collections.abc import AsyncIterator
from typing import Literal

from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from agent_runtime.api.schemas.chat import (
    DoneEventData,
    ErrorEventData,
    MessageEventData,
)
from agent_runtime.chat import ChatService, PreparedChatTurn

type EventName = Literal["message", "error", "done"]
type EventData = MessageEventData | ErrorEventData | DoneEventData


class RunStreamingResponse(StreamingResponse):
    """无论发送端如何断开，都执行一次 Active Run 后台清理。"""

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """把 Starlette 正常与异常发送路径统一到同一个清理出口。"""

        cleanup = self.background
        self.background = None
        try:
            await super().__call__(scope, receive, send)
        finally:
            if cleanup is not None:
                await cleanup()


def encode_sse(event_name: EventName, payload: EventData) -> str:
    """输出只包含事件名和 JSON 数据的单个 SSE 帧。"""

    return f"event: {event_name}\ndata: {payload.model_dump_json()}\n\n"


async def stream_chat_sse(
    chat_service: ChatService,
    turn: PreparedChatTurn,
) -> AsyncIterator[str]:
    """消费产品事件队列，并在客户端提前断开时取消 producer。"""

    await chat_service.start_producer(turn)
    terminal_event_seen = False
    try:
        while True:
            event = await turn.active_run.event_queue.get()
            if event is None:
                return
            if event.name == "done":
                terminal_event_seen = True
            yield encode_sse(event.name, event.data)
    finally:
        if not terminal_event_seen:
            await chat_service.cancel_run(
                turn.active_run,
                reason="disconnected",
            )
