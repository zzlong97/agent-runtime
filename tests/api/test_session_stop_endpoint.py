"""Stage 2 Stop 产品接口测试。"""

import asyncio
from uuid import UUID

import httpx


class FakeSessionStopService:
    """记录 Stop 调用并返回可配置结果。"""

    def __init__(self) -> None:
        self.calls: list[UUID] = []
        self.result = "stopped"
        self.error: Exception | None = None

    async def stop_session(self, *, session_id: UUID) -> str:
        self.calls.append(session_id)
        if self.error is not None:
            raise self.error
        return self.result


def _post(app, session_id: str) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                f"/api/v1/chat/sessions/{session_id}/stop"
            )

    return asyncio.run(request())


def test_stop_endpoint_returns_stopped_after_service_cleanup() -> None:
    from agent_runtime.main import create_app

    session_id = "00000000-0000-0000-0000-000000001301"
    service = FakeSessionStopService()
    app = create_app(chat_service=service)

    response = _post(app, session_id)

    assert response.status_code == 200
    assert response.json() == {
        "session_id": session_id,
        "status": "stopped",
    }
    assert service.calls == [UUID(session_id)]


def test_stop_endpoint_is_idempotent_without_active_run() -> None:
    from agent_runtime.main import create_app

    session_id = "00000000-0000-0000-0000-000000001302"
    service = FakeSessionStopService()
    service.result = "idle"
    app = create_app(chat_service=service)

    response = _post(app, session_id)

    assert response.status_code == 200
    assert response.json() == {
        "session_id": session_id,
        "status": "idle",
    }


def test_stop_endpoint_maps_session_error_before_mutation() -> None:
    from agent_runtime.core.errors import ApplicationError
    from agent_runtime.main import create_app

    session_id = "00000000-0000-0000-0000-000000001303"
    service = FakeSessionStopService()
    service.error = ApplicationError(
        code="SESSION_NOT_FOUND",
        message="Session 不存在",
        status_code=404,
    )
    app = create_app(chat_service=service)

    response = _post(app, session_id)

    assert response.status_code == 404
    assert response.json() == {
        "detail": {
            "code": "SESSION_NOT_FOUND",
            "message": "Session 不存在",
            "retryable": False,
        }
    }


def test_stop_endpoint_openapi_describes_session_parameter() -> None:
    from agent_runtime.main import create_app

    schema = create_app(chat_service=object()).openapi()
    operation = schema["paths"][
        "/api/v1/chat/sessions/{session_id}/stop"
    ]["post"]
    parameter = next(
        item for item in operation["parameters"] if item["name"] == "session_id"
    )

    assert parameter["description"] == (
        "要停止当前 Run 的 Session UUID；只允许操作固定本地用户拥有的 "
        "Session，不存在或不属于该用户时统一返回 404。"
    )
