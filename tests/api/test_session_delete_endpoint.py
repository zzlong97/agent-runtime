"""S2-08 Session 幂等硬删除 HTTP 接口测试。"""

import asyncio
from uuid import UUID

import httpx


class FakeSessionDeleteService:
    """记录 Session 删除调用并返回可配置结果。"""

    def __init__(self) -> None:
        self.calls: list[UUID] = []
        self.error: Exception | None = None

    async def delete_session(self, *, session_id: UUID) -> None:
        self.calls.append(session_id)
        if self.error is not None:
            raise self.error


def _delete(app, session_id: str) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.delete(
                f"/api/v1/chat/sessions/{session_id}"
            )

    return asyncio.run(request())


def test_session_delete_endpoint_returns_empty_204_after_cleanup() -> None:
    from agent_runtime.main import create_app

    session_id = "00000000-0000-0000-0000-000000002810"
    service = FakeSessionDeleteService()
    app = create_app(chat_service=service)

    response = _delete(app, session_id)

    assert response.status_code == 204
    assert response.content == b""
    assert service.calls == [UUID(session_id)]


def test_session_delete_endpoint_keeps_idempotent_missing_result_as_204() -> None:
    from agent_runtime.main import create_app

    session_id = "00000000-0000-0000-0000-000000002811"
    service = FakeSessionDeleteService()
    app = create_app(chat_service=service)

    first = _delete(app, session_id)
    second = _delete(app, session_id)

    assert first.status_code == 204
    assert second.status_code == 204
    assert service.calls == [UUID(session_id), UUID(session_id)]


def test_session_delete_endpoint_maps_retryable_cleanup_error() -> None:
    from agent_runtime.main import create_app
    from agent_runtime.session_deletion import SessionDeletionError

    session_id = "00000000-0000-0000-0000-000000002812"
    service = FakeSessionDeleteService()
    service.error = SessionDeletionError(
        code="SESSION_DELETE_FAILED",
        message="Session 关联数据删除失败，请重试",
        status_code=500,
        retryable=True,
    )
    app = create_app(chat_service=service)

    response = _delete(app, session_id)

    assert response.status_code == 500
    assert response.json() == {
        "detail": {
            "code": "SESSION_DELETE_FAILED",
            "message": "Session 关联数据删除失败，请重试",
            "retryable": True,
        }
    }


def test_session_delete_endpoint_rejects_invalid_uuid_before_service() -> None:
    from agent_runtime.main import create_app

    service = FakeSessionDeleteService()
    app = create_app(chat_service=service)

    response = _delete(app, "not-a-uuid")

    assert response.status_code == 422
    assert service.calls == []


def test_session_delete_openapi_describes_idempotent_product_boundary() -> None:
    from agent_runtime.main import create_app

    schema = create_app(chat_service=object()).openapi()
    operation = schema["paths"][
        "/api/v1/chat/sessions/{session_id}"
    ]["delete"]
    parameter = next(
        item for item in operation["parameters"] if item["name"] == "session_id"
    )

    assert operation["responses"]["204"]["description"] == (
        "Session 已完成幂等硬删除"
    )
    assert parameter["description"] == (
        "要硬删除的 Session UUID；仅删除固定本地用户的数据，不存在或不属于"
        "该用户时也幂等返回 204。"
    )
