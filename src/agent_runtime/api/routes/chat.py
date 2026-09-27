"""聊天 HTTP + SSE 与 Stage 2 Session 产品入口。"""

import logging
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response

from agent_runtime.api.schemas.chat import ChatCompletionRequest
from agent_runtime.api.schemas.feedback import FeedbackRequest, FeedbackResponse
from agent_runtime.api.schemas.messages import (
    MessageHistoryQuery,
    MessageHistoryResponse,
)
from agent_runtime.api.schemas.runs import (
    RegenerateRunRequest,
    RunSummaryResponse,
)
from agent_runtime.api.schemas.sessions import (
    SessionListQuery,
    SessionListResponse,
    SessionRenameRequest,
    SessionRenameResponse,
)
from agent_runtime.chat import ChatService
from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])
logger = logging.getLogger(__name__)


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

    log_business_event(
        logger,
        "业务请求失败",
        level=logging.WARNING,
        error_code=error.code,
        error_type=type(error).__name__,
        status_code=error.status_code,
        retryable=error.retryable,
    )
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


@router.delete(
    "/sessions/{session_id}",
    status_code=204,
    responses={204: {"description": "Session 已完成幂等硬删除"}},
)
async def delete_session(
    session_id: Annotated[
        UUID,
        Path(
            description=(
                "要硬删除的 Session UUID；仅删除固定本地用户的数据，不存在或不属于"
                "该用户时也幂等返回 204。"
            )
        ),
    ],
    request: Request,
) -> Response:
    """停止当前 Run 后按固定顺序幂等硬删除 Session。"""

    chat_service = _get_chat_service(request)
    try:
        await chat_service.delete_session(session_id=session_id)
    except ApplicationError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_application_error_detail(error),
        ) from error
    return Response(status_code=204)


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


@router.post(
    "/messages/{message_id}/feedback",
    response_model=FeedbackResponse,
)
async def submit_feedback(
    message_id: Annotated[
        UUID,
        Path(
            description=(
                "要反馈的公共 AIMessage 稳定 UUID；仅允许固定本地用户当前活动 Parent "
                "分支中的 completed AIMessage。"
            )
        ),
    ],
    payload: FeedbackRequest,
    request: Request,
) -> FeedbackResponse:
    """保存、替换或幂等取消当前活动回答的反馈。"""

    chat_service = _get_chat_service(request)
    try:
        result = await chat_service.submit_feedback(
            message_id=message_id,
            action=payload.action,
        )
    except ApplicationError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_application_error_detail(error),
        ) from error
    return FeedbackResponse.model_validate(result, from_attributes=True)


@router.get(
    "/sessions/{session_id}/active-run",
    response_model=RunSummaryResponse | None,
)
async def get_active_run(
    session_id: Annotated[
        UUID,
        Path(
            description=(
                "要恢复页面运行状态的 Session UUID；只允许查询固定"
                "本地用户拥有的 Session，否则统一返回 404。"
            )
        ),
    ],
    request: Request,
) -> RunSummaryResponse | None:
    """返回 Session 当前活动 Run 的公开摘要，空闲时返回 null。"""

    chat_service = _get_chat_service(request)
    try:
        run = await chat_service.get_active_run(session_id=session_id)
    except ApplicationError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_application_error_detail(error),
        ) from error
    if run is None:
        return None
    return RunSummaryResponse.model_validate(run, from_attributes=True)


@router.post(
    "/sessions/{session_id}/messages/{message_id}/regenerate",
    response_model=RunSummaryResponse,
    status_code=202,
)
async def regenerate_message(
    session_id: Annotated[
        UUID,
        Path(
            description=(
                "要重新生成最新完成回答的 Session UUID；只允许操作固定本地用户"
                "拥有的 Session，不存在或不属于该用户时统一返回 404。"
            )
        ),
    ],
    message_id: Annotated[
        UUID,
        Path(
            description=(
                "要重新生成的公共 AIMessage 稳定 UUID；必须是当前活动分支最后一条"
                "且 runtime_status 为 completed 的 AIMessage。"
            )
        ),
    ],
    payload: RegenerateRunRequest,
    request: Request,
) -> RunSummaryResponse:
    """持久化 Regenerate Run 并返回 202，执行不再绑定 HTTP 连接。"""

    chat_service = _get_chat_service(request)
    try:
        run = await chat_service.submit_regeneration_run(
            request_id=payload.request_id,
            session_id=session_id,
            message_id=message_id,
        )
    except ApplicationError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_application_error_detail(error),
        ) from error

    return RunSummaryResponse.model_validate(run, from_attributes=True)


@router.post(
    "/completions",
    response_model=RunSummaryResponse,
    status_code=202,
)
async def create_chat_completion(
    payload: ChatCompletionRequest,
    request: Request,
) -> RunSummaryResponse:
    """先持久化幂等 Run，再返回 202 公开摘要。"""

    chat_service = _get_chat_service(request)
    try:
        run = await chat_service.submit_chat_run(
            request_id=payload.request_id,
            session_id=payload.session_id,
            content=payload.message.content,
        )
    except ApplicationError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_application_error_detail(error),
        ) from error

    return RunSummaryResponse.model_validate(run, from_attributes=True)
