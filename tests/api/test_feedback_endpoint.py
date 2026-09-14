"""消息反馈 HTTP 产品接口测试。"""

import asyncio
from uuid import UUID

import httpx


class FakeFeedbackChatService:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str]] = []
        self.error: Exception | None = None

    async def submit_feedback(self, *, message_id: UUID, action: str):
        from agent_runtime.feedback import FeedbackResult

        self.calls.append((message_id, action))
        if self.error is not None:
            raise self.error
        return FeedbackResult(
            message_id=message_id,
            feedback=None if action == "cancel" else action,
        )


def _post(app, message_id: UUID, payload: dict[str, object]) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                f"/api/v1/chat/messages/{message_id}/feedback",
                json=payload,
            )

    return asyncio.run(request())


def test_feedback_endpoint_saves_like_and_returns_final_value() -> None:
    from agent_runtime.main import create_app

    message_id = UUID("00000000-0000-0000-0000-000000002710")
    service = FakeFeedbackChatService()
    app = create_app(chat_service=service)

    response = _post(app, message_id, {"action": "like"})

    assert response.status_code == 200
    assert response.json() == {
        "message_id": str(message_id),
        "feedback": "like",
    }
    assert service.calls == [(message_id, "like")]


def test_feedback_endpoint_cancel_returns_null() -> None:
    from agent_runtime.main import create_app

    message_id = UUID("00000000-0000-0000-0000-000000002711")
    service = FakeFeedbackChatService()
    app = create_app(chat_service=service)

    response = _post(app, message_id, {"action": "cancel"})

    assert response.status_code == 200
    assert response.json() == {
        "message_id": str(message_id),
        "feedback": None,
    }


def test_feedback_endpoint_maps_ineligible_message_to_client_error() -> None:
    from agent_runtime.core.errors import ApplicationError
    from agent_runtime.main import create_app

    message_id = UUID("00000000-0000-0000-0000-000000002712")
    service = FakeFeedbackChatService()
    service.error = ApplicationError(
        code="MESSAGE_FEEDBACK_NOT_ALLOWED",
        message="仅允许反馈当前活动分支中的 completed AIMessage",
        status_code=409,
    )
    app = create_app(chat_service=service)

    response = _post(app, message_id, {"action": "dislike"})

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "MESSAGE_FEEDBACK_NOT_ALLOWED",
            "message": "仅允许反馈当前活动分支中的 completed AIMessage",
            "retryable": False,
        }
    }


def test_feedback_endpoint_rejects_invalid_action_and_extra_fields() -> None:
    from agent_runtime.main import create_app

    message_id = UUID("00000000-0000-0000-0000-000000002713")
    service = FakeFeedbackChatService()
    app = create_app(chat_service=service)

    assert _post(app, message_id, {"action": "unknown"}).status_code == 422
    assert (
        _post(
            app,
            message_id,
            {"action": "like", "session_id": "internal"},
        ).status_code
        == 422
    )
    assert service.calls == []


def test_feedback_openapi_exposes_only_product_fields_with_chinese_descriptions() -> None:
    from agent_runtime.main import create_app

    app = create_app(chat_service=FakeFeedbackChatService())
    operation = app.openapi()["paths"][
        "/api/v1/chat/messages/{message_id}/feedback"
    ]["post"]
    path_parameter = operation["parameters"][0]

    assert path_parameter["name"] == "message_id"
    assert path_parameter["schema"]["description"] == (
        "要反馈的公共 AIMessage 稳定 UUID；仅允许固定本地用户当前活动 Parent "
        "分支中的 completed AIMessage。"
    )
    request_schema = operation["requestBody"]["content"]["application/json"][
        "schema"
    ]
    assert request_schema == {"$ref": "#/components/schemas/FeedbackRequest"}
    assert "session_id" not in str(request_schema)
    assert "user_id" not in str(request_schema)
