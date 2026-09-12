"""将 Parent 内部消息流转换为 Stage 1 SSE 产品协议。"""

from collections.abc import AsyncIterator
from typing import Literal

from agent_runtime.api.schemas.chat import (
    DoneEventData,
    ErrorEventData,
    MessageEventData,
)
from agent_runtime.chat import ChatService, PreparedChatTurn
from agent_runtime.core.errors import ApplicationError

type EventName = Literal["message", "error", "done"]
type EventData = MessageEventData | ErrorEventData | DoneEventData


def encode_sse(event_name: EventName, payload: EventData) -> str:
    """输出只包含事件名和 JSON 数据的单个 SSE 帧。"""

    return f"event: {event_name}\ndata: {payload.model_dump_json()}\n\n"


async def stream_chat_sse(
    chat_service: ChatService,
    turn: PreparedChatTurn,
) -> AsyncIterator[str]:
    """转换公共消息增量，并在流式错误后发送 error 与 failed done。"""

    partial_text: list[str] = []
    try:
        async for message in chat_service.stream_turn(turn):
            delta = str(message.text)
            if not delta:
                continue
            partial_text.append(delta)
            yield encode_sse(
                "message",
                MessageEventData(
                    session_id=turn.session_id,
                    message_id=turn.response_message_id,
                    delta=delta,
                ),
            )
        completion_status = await chat_service.get_completion_status(turn)
    except Exception as error:
        reported_error: Exception = error
        if partial_text:
            try:
                await chat_service.persist_incomplete(turn, "".join(partial_text))
            except Exception:
                reported_error = ApplicationError(
                    code="CHAT_INCOMPLETE_PERSIST_FAILED",
                    message="未能保存模型的部分输出",
                    retryable=False,
                )
        if isinstance(reported_error, ApplicationError):
            error_data = ErrorEventData(
                session_id=turn.session_id,
                code=reported_error.code,
                message=reported_error.message,
                retryable=reported_error.retryable,
            )
        else:
            error_data = ErrorEventData(
                session_id=turn.session_id,
                code="CHAT_RUNTIME_FAILED",
                message="聊天运行失败",
                retryable=False,
            )
        yield encode_sse("error", error_data)
        yield encode_sse(
            "done",
            DoneEventData(session_id=turn.session_id, status="failed"),
        )
        return

    yield encode_sse(
        "done",
        DoneEventData(
            session_id=turn.session_id,
            status=completion_status,
        ),
    )
