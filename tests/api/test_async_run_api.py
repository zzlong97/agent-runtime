"""Stage 2.5 异步 Run API 产品契约测试。"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx


RUN_ID = UUID("00000000-0000-0000-0000-000000002540")
REQUEST_ID = UUID("00000000-0000-0000-0000-000000002541")
SESSION_ID = UUID("00000000-0000-0000-0000-000000002542")
INPUT_MESSAGE_ID = UUID("00000000-0000-0000-0000-000000002543")
RESPONSE_MESSAGE_ID = UUID("00000000-0000-0000-0000-000000002544")
SOURCE_MESSAGE_ID = UUID("00000000-0000-0000-0000-000000002545")
INTERRUPT_ID = UUID("00000000-0000-0000-0000-000000002546")
RESUME_REQUEST_ID = UUID("00000000-0000-0000-0000-000000002547")


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
        self.event_stream_calls: list[tuple[UUID, int]] = []
        self.cancel_calls: list[UUID] = []
        self.resume_calls: list[tuple[UUID, UUID, UUID, dict]] = []
        self.pending_interrupt = None
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

    async def open_run_event_stream(self, *, run_id, after_seq):
        from agent_runtime.runtime.event_models import RuntimeEvent
        from agent_runtime.runtime.event_schemas import project_public_event

        self.event_stream_calls.append((run_id, after_seq))
        if self.error is not None:
            raise self.error

        async def stream():
            for seq, event_type, payload in (
                (4, "run.started", {"status": "running"}),
                (7, "run.completed", {"status": "completed"}),
            ):
                if seq <= after_seq:
                    continue
                yield project_public_event(
                    RuntimeEvent(
                        event_id=uuid4(),
                        run_id=run_id,
                        seq=seq,
                        event_type=event_type,
                        source="executor",
                        visibility="public",
                        payload=payload,
                        schema_version=1,
                        durability="durable",
                        created_at=datetime(2026, 9, 27, 12, 0, tzinfo=UTC),
                    )
                )

        return stream()

    async def cancel_persistent_run(self, *, run_id):
        self.cancel_calls.append(run_id)
        if self.error is not None:
            raise self.error
        self.run = _run(status="cancel_requested")
        return self.run

    async def get_pending_interrupt(self, *, run_id):
        assert run_id == RUN_ID
        if self.error is not None:
            raise self.error
        return self.pending_interrupt

    async def resume_persistent_run(
        self,
        *,
        run_id,
        interrupt_id,
        request_id,
        resume_payload,
    ):
        self.resume_calls.append(
            (run_id, interrupt_id, request_id, resume_payload)
        )
        if self.error is not None:
            raise self.error
        self.run = _run(status="running")
        return self.run


def _request(
    app,
    method: str,
    path: str,
    *,
    json=None,
    headers=None,
) -> httpx.Response:
    async def exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.request(
                method,
                path,
                json=json,
                headers=headers,
            )

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
        "recovery_attempts": 0,
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
        "recovery_attempts": 0,
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


def test_active_run_query_restores_pending_interrupt_prompt() -> None:
    from agent_runtime.main import create_app
    from agent_runtime.runtime.interrupts import RunInterrupt

    service = FakeAsyncRunService()
    service.active_run = _run(status="interrupted")
    service.pending_interrupt = RunInterrupt(
        run_id=RUN_ID,
        interrupt_id=INTERRUPT_ID,
        status="pending",
        interrupt_payload={"prompt": "是否允许继续执行？"},
        resume_payload=None,
        resume_request_id=None,
        resume_request_fingerprint=None,
        created_at=datetime(2026, 9, 29, 10, 0, tzinfo=UTC),
        resumed_at=None,
        cancelled_at=None,
    )
    app = create_app(chat_service=service)

    response = _request(
        app,
        "GET",
        f"/api/v1/chat/sessions/{SESSION_ID}/active-run",
    )

    assert response.status_code == 200
    assert response.json()["pending_interrupt"] == {
        "interrupt_id": str(INTERRUPT_ID),
        "interrupt_payload": {"prompt": "是否允许继续执行？"},
        "created_at": "2026-09-29T10:00:00Z",
    }


def test_resume_uses_same_run_and_returns_accepted_summary() -> None:
    from agent_runtime.main import create_app

    service = FakeAsyncRunService()
    app = create_app(chat_service=service)
    payload = {
        "interrupt_id": str(INTERRUPT_ID),
        "request_id": str(RESUME_REQUEST_ID),
        "resume_payload": {"approved": True},
    }

    response = _request(
        app,
        "POST",
        f"/api/v1/chat/runs/{RUN_ID}/resume",
        json=payload,
    )

    assert response.status_code == 202
    assert response.json() == {
        "run_id": str(RUN_ID),
        "session_id": str(SESSION_ID),
        "response_message_id": str(RESPONSE_MESSAGE_ID),
        "status": "running",
        "recovery_attempts": 0,
    }
    assert service.resume_calls == [
        (
            RUN_ID,
            INTERRUPT_ID,
            RESUME_REQUEST_ID,
            {"approved": True},
        )
    ]


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


def test_run_cancel_persists_request_and_returns_accepted_summary() -> None:
    from agent_runtime.main import create_app

    service = FakeAsyncRunService()
    app = create_app(chat_service=service)

    first = _request(
        app,
        "POST",
        f"/api/v1/chat/runs/{RUN_ID}/cancel",
    )
    repeated = _request(
        app,
        "POST",
        f"/api/v1/chat/runs/{RUN_ID}/cancel",
    )

    assert first.status_code == 202
    assert repeated.status_code == 202
    assert first.json() == repeated.json() == {
        "run_id": str(RUN_ID),
        "session_id": str(SESSION_ID),
        "response_message_id": str(RESPONSE_MESSAGE_ID),
        "status": "cancel_requested",
        "recovery_attempts": 0,
    }
    assert service.cancel_calls == [RUN_ID, RUN_ID]


def test_run_cancel_openapi_describes_run_identifier() -> None:
    from agent_runtime.main import create_app

    operation = create_app(chat_service=FakeAsyncRunService()).openapi()[
        "paths"
    ]["/api/v1/chat/runs/{run_id}/cancel"]["post"]
    parameters = {item["name"]: item for item in operation["parameters"]}

    assert set(parameters) == {"run_id"}
    assert parameters["run_id"].get("description")


def test_run_api_schema_describes_every_request_and_response_field() -> None:
    from agent_runtime.main import create_app

    schema = create_app(chat_service=FakeAsyncRunService()).openapi()
    component_names = (
        "ChatCompletionRequest",
        "RegenerateRunRequest",
        "ResumeRunRequest",
        "PendingInterruptResponse",
        "RunSummaryResponse",
    )
    for component_name in component_names:
        properties = schema["components"]["schemas"][component_name][
            "properties"
        ]
        assert properties
        assert all(property_schema.get("description") for property_schema in properties.values())


def test_run_events_get_uses_last_event_id_before_after_seq() -> None:
    from agent_runtime.main import create_app

    service = FakeAsyncRunService()
    app = create_app(chat_service=service)
    response = _request(
        app,
        "GET",
        f"/api/v1/chat/runs/{RUN_ID}/events?after_seq=1",
        headers={"Last-Event-ID": "4"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert service.event_stream_calls == [(RUN_ID, 4)]
    assert "id: 4\n" not in response.text
    assert "id: 7\n" in response.text
    assert "event: run.completed\n" in response.text


def test_run_events_rejects_invalid_last_event_id_before_streaming() -> None:
    from agent_runtime.main import create_app

    service = FakeAsyncRunService()
    app = create_app(chat_service=service)
    response = _request(
        app,
        "GET",
        f"/api/v1/chat/runs/{RUN_ID}/events?after_seq=1",
        headers={"Last-Event-ID": "invalid"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "RUN_EVENT_CURSOR_INVALID"
    assert service.event_stream_calls == []


def test_run_events_openapi_describes_path_query_and_header_parameters() -> None:
    from agent_runtime.main import create_app

    operation = create_app(chat_service=FakeAsyncRunService()).openapi()[
        "paths"
    ]["/api/v1/chat/runs/{run_id}/events"]["get"]
    parameters = {item["name"]: item for item in operation["parameters"]}

    assert set(parameters) == {"run_id", "after_seq", "Last-Event-ID"}
    assert all(item.get("description") for item in parameters.values())
