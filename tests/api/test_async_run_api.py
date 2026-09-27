"""Stage 2.5 异步 Run API 产品契约测试。"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import httpx


RUN_ID = UUID("00000000-0000-0000-0000-000000002540")
REQUEST_ID = UUID("00000000-0000-0000-0000-000000002541")
SESSION_ID = UUID("00000000-0000-0000-0000-000000002542")
INPUT_MESSAGE_ID = UUID("00000000-0000-0000-0000-000000002543")
RESPONSE_MESSAGE_ID = UUID("00000000-0000-0000-0000-000000002544")
SOURCE_MESSAGE_ID = UUID("00000000-0000-0000-0000-000000002545")


def _run(*, status: str = "queued"):
    from agent_runtime.runtime.models import Run

    now = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
    return Run(
        run_id=RUN_ID,
        request_id=REQUEST_ID,
        session_id=SESSION_ID,
        thread_id=str(SESSION_ID),
        parent_run_id=None,
        run_type="normal",
        input_message_id=INPUT_MESSAGE_ID,
        response_message_id=RESPONSE_MESSAGE_ID,
        start_checkpoint_id=None,
        input_payload={"message": {"content": "不应返回的正文"}},
        request_fingerprint="a" * 64,
        status=status,
        recovery_attempts=0,
        seq_high_watermark=0,
        error_code=None,
        error_message=None,
        created_at=now,
        started_at=None,
        finished_at=None,
        updated_at=now,
    )


class FakeAsyncRunService:
    def __init__(self) -> None:
        self.run = _run()
        self.active_run = self.run
        self.normal_calls: list[tuple[UUID, UUID | None, str]] = []
        self.regenerate_calls: list[tuple[UUID, UUID, UUID]] = []
        self.active_calls: list[UUID] = []
        self.error: Exception | None = None

    async def submit_chat_run(self, *, request_id, session_id, content):
        self.normal_calls.append((request_id, session_id, content))
        if self.error is not None:
            raise self.error
        return self.run

    async def submit_regeneration_run(
        self,
        *,
        request_id,
        session_id,
        message_id,
    ):
        self.regenerate_calls.append((request_id, session_id, message_id))
        if self.error is not None:
            raise self.error
        return self.run

    async def get_active_run(self, *, session_id):
        self.active_calls.append(session_id)
        if self.error is not None:
            raise self.error
        return self.active_run


def _request(app, method: str, path: str, *, json=None) -> httpx.Response:
    async def exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, json=json)

    return asyncio.run(exercise())


def test_normal_message_returns_accepted_persisted_run_summary() -> None:
    from agent_runtime.main import create_app

    service = FakeAsyncRunService()
    app = create_app(chat_service=service)

    response = _request(
        app,
        "POST",
        "/api/v1/chat/completions",
        json={
            "request_id": str(REQUEST_ID),
            "session_id": str(SESSION_ID),
            "message": {"content": "提交异步 Run"},
        },
    )

    assert response.status_code == 202
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "run_id": str(RUN_ID),
        "session_id": str(SESSION_ID),
        "response_message_id": str(RESPONSE_MESSAGE_ID),
        "status": "queued",
    }
    assert service.normal_calls == [
        (REQUEST_ID, SESSION_ID, "提交异步 Run")
    ]
    assert "不应返回的正文" not in response.text


def test_normal_message_requires_global_request_id() -> None:
    from agent_runtime.main import create_app

    app = create_app(chat_service=FakeAsyncRunService())
    response = _request(
        app,
        "POST",
        "/api/v1/chat/completions",
        json={"message": {"content": "缺少幂等标识"}},
    )

    assert response.status_code == 422


def test_regenerate_returns_accepted_run_summary() -> None:
    from agent_runtime.main import create_app

    service = FakeAsyncRunService()
    app = create_app(chat_service=service)
    response = _request(
        app,
        "POST",
        (
            f"/api/v1/chat/sessions/{SESSION_ID}/messages/"
            f"{SOURCE_MESSAGE_ID}/regenerate"
        ),
        json={"request_id": str(REQUEST_ID)},
    )

    assert response.status_code == 202
    assert response.json()["run_id"] == str(RUN_ID)
    assert response.json()["response_message_id"] == str(
        RESPONSE_MESSAGE_ID
    )
    assert service.regenerate_calls == [
        (REQUEST_ID, SESSION_ID, SOURCE_MESSAGE_ID)
    ]


def test_active_run_query_supports_page_refresh_without_private_fields() -> None:
    from agent_runtime.main import create_app

    service = FakeAsyncRunService()
    service.active_run = _run(status="running")
    app = create_app(chat_service=service)
    response = _request(
        app,
        "GET",
        f"/api/v1/chat/sessions/{SESSION_ID}/active-run",
    )

    assert response.status_code == 200
    assert response.json() == {
        "run_id": str(RUN_ID),
        "session_id": str(SESSION_ID),
        "response_message_id": str(RESPONSE_MESSAGE_ID),
        "status": "running",
    }
    assert service.active_calls == [SESSION_ID]
    assert "input_payload" not in response.text
    assert "checkpoint" not in response.text.lower()


def test_active_run_query_returns_null_when_session_is_idle() -> None:
    from agent_runtime.main import create_app

    service = FakeAsyncRunService()
    service.active_run = None
    app = create_app(chat_service=service)
    response = _request(
        app,
        "GET",
        f"/api/v1/chat/sessions/{SESSION_ID}/active-run",
    )

    assert response.status_code == 200
    assert response.json() is None


def test_run_api_maps_application_error_without_leaking_private_details() -> None:
    from agent_runtime.core.errors import ApplicationError
    from agent_runtime.main import create_app

    service = FakeAsyncRunService()
    service.error = ApplicationError(
        code="RUN_REQUEST_CONFLICT",
        message="request_id 已用于不同请求",
        status_code=409,
    )
    app = create_app(chat_service=service)
    response = _request(
        app,
        "POST",
        "/api/v1/chat/completions",
        json={
            "request_id": str(REQUEST_ID),
            "message": {"content": "冲突请求"},
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "RUN_REQUEST_CONFLICT",
            "message": "request_id 已用于不同请求",
            "retryable": False,
        }
    }


def test_public_session_stop_route_is_removed() -> None:
    from agent_runtime.main import create_app

    app = create_app(chat_service=FakeAsyncRunService())
    response = _request(
        app,
        "POST",
        f"/api/v1/chat/sessions/{SESSION_ID}/stop",
    )

    assert response.status_code == 404
    assert (
        f"/api/v1/chat/sessions/{{session_id}}/stop"
        not in app.openapi()["paths"]
    )


def test_run_api_schema_describes_every_request_and_response_field() -> None:
    from agent_runtime.main import create_app

    schema = create_app(chat_service=FakeAsyncRunService()).openapi()
    component_names = (
        "ChatCompletionRequest",
        "RegenerateRunRequest",
        "RunSummaryResponse",
    )
    for component_name in component_names:
        properties = schema["components"]["schemas"][component_name][
            "properties"
        ]
        assert properties
        assert all(property_schema.get("description") for property_schema in properties.values())
