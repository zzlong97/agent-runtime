"""Session 列表 HTTP 产品接口测试。"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import httpx


class FakeSessionListService:
    def __init__(self) -> None:
        from agent_runtime.sessions.models import Session, SessionPage

        now = datetime(2026, 9, 13, 9, 0, tzinfo=UTC)
        self.page = SessionPage(
            items=(
                Session(
                    session_id=UUID(
                        "00000000-0000-0000-0000-000000000402"
                    ),
                    user_id="configured-user",
                    title="较新的会话",
                    created_at=now,
                    updated_at=now,
                ),
            ),
            next_cursor="next-page-token",
        )
        self.calls: list[tuple[str | None, int]] = []
        self.error: Exception | None = None

    async def list_sessions(self, *, cursor: str | None, limit: int):
        self.calls.append((cursor, limit))
        if self.error is not None:
            raise self.error
        return self.page


def _get(app, params: dict[str, object] | None = None) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get("/api/v1/chat/sessions", params=params)

    return asyncio.run(request())


def test_session_list_endpoint_returns_product_dto_with_defaults() -> None:
    from agent_runtime.main import create_app

    service = FakeSessionListService()
    app = create_app(chat_service=service)

    response = _get(app)

    assert response.status_code == 200
    assert service.calls == [(None, 20)]
    assert response.json() == {
        "items": [
            {
                "session_id": "00000000-0000-0000-0000-000000000402",
                "title": "较新的会话",
                "created_at": "2026-09-13T09:00:00Z",
                "updated_at": "2026-09-13T09:00:00Z",
            }
        ],
        "next_cursor": "next-page-token",
    }
    assert "user_id" not in response.text


def test_session_list_endpoint_passes_opaque_cursor_and_limit() -> None:
    from agent_runtime.main import create_app

    service = FakeSessionListService()
    app = create_app(chat_service=service)

    response = _get(app, {"cursor": "opaque-token", "limit": 7})

    assert response.status_code == 200
    assert service.calls == [("opaque-token", 7)]


def test_session_list_endpoint_maps_invalid_cursor_to_explicit_client_error() -> None:
    from agent_runtime.core.errors import ApplicationError
    from agent_runtime.main import create_app

    service = FakeSessionListService()
    service.error = ApplicationError(
        code="SESSION_CURSOR_INVALID",
        message="Session 列表游标无效",
        status_code=400,
    )
    app = create_app(chat_service=service)

    response = _get(app, {"cursor": "broken"})

    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "SESSION_CURSOR_INVALID",
            "message": "Session 列表游标无效",
            "retryable": False,
        }
    }


def test_session_list_endpoint_rejects_limit_outside_stage_two_range() -> None:
    from agent_runtime.main import create_app

    service = FakeSessionListService()
    app = create_app(chat_service=service)

    response = _get(app, {"limit": 101})

    assert response.status_code == 422
    assert service.calls == []


def test_session_list_endpoint_rejects_client_supplied_user_id() -> None:
    from agent_runtime.main import create_app

    service = FakeSessionListService()
    app = create_app(chat_service=service)

    response = _get(app, {"user_id": "client-user"})

    assert response.status_code == 422
    assert service.calls == []
