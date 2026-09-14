"""聊天 HTTP + SSE 与 Stage 2 Session 产品入口。"""

from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Request
from starlette.background import BackgroundTask

from agent_runtime.api.schemas.chat import ChatCompletionRequest
from agent_runtime.api.schemas.messages import (
    MessageHistoryQuery,
    MessageHistoryResponse,
)
from agent_runtime.api.schemas.sessions import (
    SessionListQuery,
    SessionListResponse,
    SessionRenameRequest,
    SessionRenameResponse,
    SessionStopResponse,
)
from agent_runtime.chat import ChatService
from agent_runtime.core.errors import ApplicationError
from agent_runtime.streaming.sse import RunStreamingResponse, stream_chat_sse

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


@router.patch(
    "/sessions/{session_id}/rename",
    response_model=SessionRenameResponse,
)
async def rename_session(
    session_id: Annotated[
        UUID,
        Path(
            description=(
                "要改名的 Session UUID；只允许操作固定本地用户拥有的 "
                "Session，不存在或不属于该用户时统一返回 404。"
            )
        ),
    ],
    payload: SessionRenameRequest,
    request: Request,
) -> SessionRenameResponse:
    """更新固定本地用户拥有的 Session 标题。"""

    chat_service = _get_chat_service(request)
    try:
        session = await chat_service.rename_session(
            session_id=session_id,
            title=payload.title,
        )
    except ApplicationError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_application_error_detail(error),
        ) from error
    return SessionRenameResponse.model_validate(session, from_attributes=True)


@router.post(
    "/sessions/{session_id}/stop",
    response_model=SessionStopResponse,
)
async def stop_session(
    session_id: Annotated[
        UUID,
        Path(
            description=(
                "要停止当前 Run 的 Session UUID；只允许操作固定本地用户拥有的 "
                "Session，不存在或不属于该用户时统一返回 404。"
            )
        ),
    ],
    request: Request,
) -> SessionStopResponse:
    """停止当前 active Run；无运行时返回幂等 idle 结果。"""

    chat_service = _get_chat_service(request)
    try:
        status = await chat_service.stop_session(session_id=session_id)
    except ApplicationError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_application_error_detail(error),
        ) from error
    return SessionStopResponse(session_id=session_id, status=status)


@router.get(
    "/sessions/{session_id}/messages",
    response_model=MessageHistoryResponse,
)
async def list_messages(
    session_id: Annotated[
        UUID,
        Path(
            description=(
                "要读取当前活动 Parent 分支历史的 Session UUID；只允许读取固定"
                "本地用户拥有的 Session，不存在或不属于该用户时统一返回 404。"
            )
        ),
    ],
    request: Request,
    query: Annotated[MessageHistoryQuery, Query()],
) -> MessageHistoryResponse:
    """返回过滤内部状态后的当前活动公共消息。"""

    chat_service = _get_chat_service(request)
    try:
        page = await chat_service.list_messages(
            session_id=session_id,
            before=query.before,
            limit=query.limit,
        )
    except ApplicationError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_application_error_detail(error),
        ) from error
    return MessageHistoryResponse.model_validate(page, from_attributes=True)


@router.post("/completions")
async def create_chat_completion(
    payload: ChatCompletionRequest,
    request: Request,
) -> RunStreamingResponse:
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

    return RunStreamingResponse(
        stream_chat_sse(chat_service, turn),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
        background=BackgroundTask(
            chat_service.cancel_run,
            turn.active_run,
            reason="disconnected",
        ),
    )
