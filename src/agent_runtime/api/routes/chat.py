"""聊天 HTTP + SSE 与 Stage 2 Session 产品入口。"""

from typing import Annotated, Any, cast

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from agent_runtime.api.schemas.chat import ChatCompletionRequest
from agent_runtime.api.schemas.sessions import SessionListQuery, SessionListResponse
from agent_runtime.chat import ChatService
from agent_runtime.core.errors import ApplicationError
from agent_runtime.streaming.sse import stream_chat_sse

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


def _get_chat_service(request: Request) -> ChatService:
    """读取应用生命周期中创建或测试注入的 ChatService。"""

    service = getattr(request.app.state, "chat_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "CHAT_SERVICE_UNAVAILABLE",
                "message": "聊天服务尚未完成初始化",
                "retryable": True,
            },
        )
    return cast(ChatService, service)


def _application_error_detail(error: ApplicationError) -> dict[str, Any]:
    """把应用错误转换为稳定的 HTTP JSON 错误字段。"""

    return {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
    }


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    request: Request,
    query: Annotated[SessionListQuery, Query()],
) -> SessionListResponse:
    """按稳定复合顺序返回固定本地用户的一页 Session。"""

    chat_service = _get_chat_service(request)
    try:
        page = await chat_service.list_sessions(
            cursor=query.cursor,
            limit=query.limit,
        )
    except ApplicationError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_application_error_detail(error),
        ) from error
    return SessionListResponse.model_validate(page, from_attributes=True)


@router.post("/completions")
async def create_chat_completion(
    payload: ChatCompletionRequest,
    request: Request,
) -> StreamingResponse:
    """先完成请求与 Session 校验，再建立产品级 SSE 响应。"""

    chat_service = _get_chat_service(request)
    try:
        turn = await chat_service.prepare_turn(
            session_id=payload.session_id,
            content=payload.message.content,
        )
    except ApplicationError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_application_error_detail(error),
        ) from error

    return StreamingResponse(
        stream_chat_sse(chat_service, turn),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
