"""历史消息 HTTP 产品接口测试。"""

import asyncio
from uuid import UUID

import httpx


class FakeMessageHistoryService:
    def __init__(self) -> None:
        from agent_runtime.history import MessagePage, ProductMessage

        self.page = MessagePage(
            items=(
                ProductMessage(
                    message_id=UUID(
                        "00000000-0000-0000-0000-000000001601"
                    ),
                    role="user",
                    content="你好",
                    runtime_status=None,
                    capability_id=None,
                    feedback=None,
                ),
                ProductMessage(
                    message_id=UUID(
                        "00000000-0000-0000-0000-000000001602"
                    ),
                    role="assistant",
                    content="你好！",
                    runtime_status="completed",
                    capability_id="weather_lookup",
                    feedback=None,
                ),
            ),
            next_before=UUID(
                "00000000-0000-0000-0000-000000001601"
            ),
        )
        self.calls: list[tuple[UUID, UUID | None, int]] = []
        self.error: Exception | None = None

    async def list_messages(
        self,
        *,
        session_id: UUID,
        before: UUID | None,
        limit: int,
    ):
        self.calls.append((session_id, before, limit))
        if self.error is not None:
            raise self.error
        return self.page


def _get(app, session_id: UUID, params=None) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get(
                f"/api/v1/chat/sessions/{session_id}/messages",
                params=params,
            )

    return asyncio.run(request())


def test_message_history_endpoint_returns_only_product_dto_with_defaults() -> None:
    from agent_runtime.main import create_app

    session_id = UUID("00000000-0000-0000-0000-000000001600")
    service = FakeMessageHistoryService()
    app = create_app(chat_service=service)

    response = _get(app, session_id)

    assert response.status_code == 200
    assert service.calls == [(session_id, None, 50)]
    assert response.json() == {
        "items": [
            {
                "message_id": "00000000-0000-0000-0000-000000001601",
                "role": "user",
                "content": "你好",
                "runtime_status": None,
                "capability_id": None,
                "feedback": None,
            },
            {
                "message_id": "00000000-0000-0000-0000-000000001602",
                "role": "assistant",
                "content": "你好！",
                "runtime_status": "completed",
                "capability_id": "weather_lookup",
                "feedback": None,
            },
        ],
        "next_before": "00000000-0000-0000-0000-000000001601",
    }
    for internal_name in (
        "checkpoint",
        "state_snapshot",
        "node",
        "tasks",
        "additional_kwargs",
    ):
        assert internal_name not in response.text.lower()


def test_message_history_endpoint_passes_before_and_limit() -> None:
    from agent_runtime.main import create_app

    session_id = UUID("00000000-0000-0000-0000-000000001610")
    before = UUID("00000000-0000-0000-0000-000000001611")
    service = FakeMessageHistoryService()
    app = create_app(chat_service=service)

    response = _get(app, session_id, {"before": str(before), "limit": 7})

    assert response.status_code == 200
    assert service.calls == [(session_id, before, 7)]


def test_message_history_endpoint_maps_invalid_before_to_client_error() -> None:
    from agent_runtime.core.errors import ApplicationError
    from agent_runtime.main import create_app

    session_id = UUID("00000000-0000-0000-0000-000000001620")
    service = FakeMessageHistoryService()
    service.error = ApplicationError(
        code="MESSAGE_BEFORE_INVALID",
        message="before 不是当前活动历史中的消息",
        status_code=400,
    )
    app = create_app(chat_service=service)

    response = _get(
        app,
        session_id,
        {"before": "00000000-0000-0000-0000-000000001699"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "MESSAGE_BEFORE_INVALID",
            "message": "before 不是当前活动历史中的消息",
            "retryable": False,
        }
    }


def test_message_history_endpoint_rejects_invalid_or_extra_query_parameters() -> None:
    from agent_runtime.main import create_app

    session_id = UUID("00000000-0000-0000-0000-000000001630")
    service = FakeMessageHistoryService()
    app = create_app(chat_service=service)

    assert _get(app, session_id, {"limit": 101}).status_code == 422
    assert _get(app, session_id, {"before": "not-a-uuid"}).status_code == 422
    assert _get(app, session_id, {"branch_id": "internal"}).status_code == 422
    assert service.calls == []
