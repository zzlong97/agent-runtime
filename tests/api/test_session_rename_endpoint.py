"""Session 改名 HTTP 产品接口测试。"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import httpx


class FakeSessionRenameService:
    def __init__(self) -> None:
        from agent_runtime.sessions.models import Session

        self.session_id = UUID("00000000-0000-0000-0000-000000000901")
        self.session = Session(
            session_id=self.session_id,
            user_id="configured-user",
            title="新标题",
            created_at=datetime(2026, 9, 13, 10, 0, tzinfo=UTC),
            updated_at=datetime(2026, 9, 13, 11, 0, tzinfo=UTC),
        )
        self.calls: list[tuple[UUID, str]] = []
        self.error: Exception | None = None

    async def rename_session(self, *, session_id: UUID, title: str):
        self.calls.append((session_id, title))
        if self.error is not None:
            raise self.error
        return self.session


def _patch(app, session_id: str, payload: dict[str, object]) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.patch(
                f"/api/v1/chat/sessions/{session_id}/rename",
                json=payload,
            )

    return asyncio.run(request())


def test_session_rename_endpoint_trims_persists_and_returns_product_dto() -> None:
    from agent_runtime.main import create_app

    service = FakeSessionRenameService()
    app = create_app(chat_service=service)

    response = _patch(
        app,
        str(service.session_id),
        {"title": "  新标题\n"},
    )

    assert response.status_code == 200
    assert service.calls == [(service.session_id, "新标题")]
    assert response.json() == {
        "session_id": str(service.session_id),
        "title": "新标题",
        "created_at": "2026-09-13T10:00:00Z",
        "updated_at": "2026-09-13T11:00:00Z",
    }
    assert "user_id" not in response.text


def test_session_rename_endpoint_maps_missing_or_unowned_session_to_404() -> None:
    from agent_runtime.core.errors import ApplicationError
    from agent_runtime.main import create_app

    service = FakeSessionRenameService()
    service.error = ApplicationError(
        code="SESSION_NOT_FOUND",
        message="Session 不存在",
        status_code=404,
    )
    app = create_app(chat_service=service)

    response = _patch(
        app,
        str(service.session_id),
        {"title": "新标题"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "detail": {
            "code": "SESSION_NOT_FOUND",
            "message": "Session 不存在",
            "retryable": False,
        }
    }


def test_session_rename_endpoint_rejects_invalid_payload_before_service_call() -> None:
    from agent_runtime.main import create_app

    invalid_payloads = [
        {"title": "   "},
        {"title": "新" * 101},
        {"title": "合法标题", "unexpected": True},
        {"title": "合法标题", "user_id": "client-user"},
    ]

    for payload in invalid_payloads:
        service = FakeSessionRenameService()
        app = create_app(chat_service=service)

        response = _patch(app, str(service.session_id), payload)

        assert response.status_code == 422
        assert service.calls == []


def test_session_rename_endpoint_rejects_invalid_session_uuid() -> None:
    from agent_runtime.main import create_app

    service = FakeSessionRenameService()
    app = create_app(chat_service=service)

    response = _patch(app, "not-a-uuid", {"title": "新标题"})

    assert response.status_code == 422
    assert service.calls == []


def test_session_rename_openapi_describes_session_id_parameter() -> None:
    from agent_runtime.main import create_app

    schema = create_app(chat_service=object()).openapi()
    operation = schema["paths"][
        "/api/v1/chat/sessions/{session_id}/rename"
    ]["patch"]

    assert operation["parameters"] == [
        {
            "name": "session_id",
            "in": "path",
            "required": True,
            "schema": {
                "type": "string",
                "format": "uuid",
                "description": (
                    "要改名的 Session UUID；只允许操作固定本地用户拥有的 "
                    "Session，不存在或不属于该用户时统一返回 404。"
                ),
                "title": "Session Id",
            },
            "description": (
                "要改名的 Session UUID；只允许操作固定本地用户拥有的 "
                "Session，不存在或不属于该用户时统一返回 404。"
            ),
        }
    ]
