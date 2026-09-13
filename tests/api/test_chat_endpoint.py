"""Stage 1 聊天 HTTP + SSE 端点测试。"""

import asyncio
import json
from uuid import UUID

import httpx
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage


def _parse_sse(response_text: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for frame in response_text.strip().split("\n\n"):
        lines = frame.splitlines()
        events.append(
            (
                lines[0].removeprefix("event: "),
                json.loads(lines[1].removeprefix("data: ")),
            )
        )
    return events


class FakeChatService:
    def __init__(self) -> None:
        from agent_runtime.chat import ChatService
        from agent_runtime.core.config import Settings
        from agent_runtime.runs import ActiveRunRegistry

        self.session_id = UUID("00000000-0000-0000-0000-000000000801")
        self.message_id = UUID("00000000-0000-0000-0000-000000000803")
        self._run_registry = ActiveRunRegistry()
        self._producer_service = ChatService(
            settings=Settings(_env_file=None),
            session_repository=object(),
            session_service=self,
            parent_graph=object(),
            run_registry=self._run_registry,
        )
        self.events: list[object] = []
        self.status = "completed"
        self.prepare_error: Exception | None = None
        self.prepare_calls: list[tuple[UUID | None, str]] = []
        self.incomplete_contents: list[str] = []
        self.persist_error: Exception | None = None
        self.last_turn = None
        self.stream_blocker: asyncio.Event | None = None
        self.touched_session_ids: list[UUID] = []

    async def touch_session(self, *, session_id: UUID) -> None:
        self.touched_session_ids.append(session_id)

    async def prepare_turn(self, *, session_id, content):
        from agent_runtime.chat import PreparedChatTurn

        self.prepare_calls.append((session_id, content))
        if self.prepare_error is not None:
            raise self.prepare_error
        active_run = await self._run_registry.reserve(
            self.session_id,
            self.message_id,
        )
        self.last_turn = PreparedChatTurn(
            session_id=self.session_id,
            human_message=HumanMessage(
                content=content,
                id="00000000-0000-0000-0000-000000000802",
            ),
            response_message_id=self.message_id,
            config={
                "configurable": {
                    "thread_id": str(self.session_id),
                    "message_id": str(self.message_id),
                }
            },
            active_run=active_run,
        )
        return self.last_turn

    async def stream_turn(self, turn):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event
        if self.stream_blocker is not None:
            await self.stream_blocker.wait()

    async def get_completion_status(self, turn):
        return self.status

    async def persist_incomplete(self, turn, content):
        self.incomplete_contents.append(content)
        if self.persist_error is not None:
            raise self.persist_error

    async def start_producer(self, turn):
        self._producer_service.stream_turn = self.stream_turn
        self._producer_service.get_completion_status = self.get_completion_status
        self._producer_service.persist_incomplete = self.persist_incomplete
        await self._producer_service.start_producer(turn)

    async def cancel_run(self, run, *, reason):
        return await self._producer_service.cancel_run(run, reason=reason)


def _post(app, payload: dict[str, object]) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post("/api/v1/chat/completions", json=payload)

    return asyncio.run(request())


def test_chat_endpoint_streams_message_deltas_with_stable_ids() -> None:
    from agent_runtime.main import create_app

    service = FakeChatService()
    service.events = [AIMessageChunk(content="你"), AIMessageChunk(content="好")]
    app = create_app(chat_service=service)

    response = _post(app, {"message": {"content": "测试消息"}})

    events = _parse_sse(response.text)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert [event_name for event_name, _data in events] == [
        "message",
        "message",
        "done",
    ]
    assert [data["delta"] for _event, data in events[:2]] == ["你", "好"]
    assert {data["session_id"] for _event, data in events} == {
        str(service.session_id)
    }
    assert {data["message_id"] for _event, data in events[:2]} == {
        str(service.message_id)
    }
    assert events[-1][1]["status"] == "completed"
    assert "node" not in response.text
    assert "checkpoint" not in response.text


def test_chat_endpoint_maps_unsupported_to_message_then_done() -> None:
    from agent_runtime.main import create_app

    service = FakeChatService()
    service.events = [AIMessage(content="当前能力不支持")]
    service.status = "unsupported"
    app = create_app(chat_service=service)

    response = _post(app, {"message": {"content": "不支持的请求"}})

    events = _parse_sse(response.text)
    assert [event_name for event_name, _data in events] == ["message", "done"]
    assert events[0][1]["delta"] == "当前能力不支持"
    assert events[1][1]["status"] == "unsupported"
    assert service.incomplete_contents == []


def test_chat_endpoint_emits_error_then_failed_and_persists_partial_output() -> None:
    from agent_runtime.core.errors import ApplicationError
    from agent_runtime.main import create_app

    service = FakeChatService()
    service.events = [
        AIMessageChunk(content="部分"),
        ApplicationError(
            code="MODEL_CALL_FAILED",
            message="模型调用失败",
            retryable=True,
        ),
    ]
    app = create_app(chat_service=service)

    response = _post(app, {"message": {"content": "触发失败"}})

    events = _parse_sse(response.text)
    assert [event_name for event_name, _data in events] == [
        "message",
        "error",
        "done",
    ]
    assert events[1][1] == {
        "session_id": str(service.session_id),
        "code": "MODEL_CALL_FAILED",
        "message": "模型调用失败",
        "retryable": True,
    }
    assert events[2][1]["status"] == "failed"
    assert service.incomplete_contents == ["部分"]


def test_chat_endpoint_hides_unexpected_runtime_error_details() -> None:
    from agent_runtime.main import create_app

    service = FakeChatService()
    service.events = [RuntimeError("secret provider detail")]
    app = create_app(chat_service=service)

    response = _post(app, {"message": {"content": "触发异常"}})

    events = _parse_sse(response.text)
    assert [event_name for event_name, _data in events] == ["error", "done"]
    assert events[0][1]["code"] == "CHAT_RUNTIME_FAILED"
    assert events[0][1]["message"] == "聊天运行失败"
    assert events[1][1]["status"] == "failed"
    assert "secret provider detail" not in response.text
    assert service.incomplete_contents == []


def test_chat_endpoint_still_finishes_when_incomplete_persistence_fails() -> None:
    from agent_runtime.main import create_app

    service = FakeChatService()
    service.events = [
        AIMessageChunk(content="部分"),
        RuntimeError("provider disconnected"),
    ]
    service.persist_error = RuntimeError("database unavailable")
    app = create_app(chat_service=service)

    response = _post(app, {"message": {"content": "触发双重失败"}})

    events = _parse_sse(response.text)
    assert [event_name for event_name, _data in events] == [
        "message",
        "error",
        "done",
    ]
    assert events[1][1]["code"] == "CHAT_INCOMPLETE_PERSIST_FAILED"
    assert events[1][1]["message"] == "未能保存模型的部分输出"
    assert events[2][1]["status"] == "failed"


def test_chat_endpoint_returns_session_validation_error_before_sse() -> None:
    from agent_runtime.core.errors import ApplicationError
    from agent_runtime.main import create_app

    service = FakeChatService()
    service.prepare_error = ApplicationError(
        code="SESSION_NOT_FOUND",
        message="Session 不存在",
        status_code=404,
    )
    app = create_app(chat_service=service)
    session_id = "00000000-0000-0000-0000-000000000899"

    response = _post(
        app,
        {"session_id": session_id, "message": {"content": "继续"}},
    )

    assert response.status_code == 404
    assert not response.headers["content-type"].startswith("text/event-stream")
    assert response.json() == {
        "detail": {
            "code": "SESSION_NOT_FOUND",
            "message": "Session 不存在",
            "retryable": False,
        }
    }


def test_chat_endpoint_returns_session_busy_before_sse() -> None:
    from agent_runtime.runs import SessionBusyError
    from agent_runtime.main import create_app

    service = FakeChatService()
    service.prepare_error = SessionBusyError(
        code="SESSION_BUSY",
        message="当前 Session 正在生成回复",
        status_code=409,
        retryable=True,
    )
    app = create_app(chat_service=service)

    response = _post(
        app,
        {
            "session_id": str(service.session_id),
            "message": {"content": "重复请求"},
        },
    )

    assert response.status_code == 409
    assert not response.headers["content-type"].startswith("text/event-stream")
    assert response.json() == {
        "detail": {
            "code": "SESSION_BUSY",
            "message": "当前 Session 正在生成回复",
            "retryable": True,
        }
    }


def test_chat_endpoint_rejects_invalid_request_before_service_call() -> None:
    from agent_runtime.main import create_app

    service = FakeChatService()
    app = create_app(chat_service=service)

    response = _post(app, {"message": {"content": "   "}})

    assert response.status_code == 422
    assert not response.headers["content-type"].startswith("text/event-stream")
    assert service.prepare_calls == []


def test_chat_response_background_releases_run_if_body_never_starts() -> None:
    from fastapi import Request

    from agent_runtime.api.routes.chat import create_chat_completion
    from agent_runtime.api.schemas.chat import ChatCompletionRequest
    from agent_runtime.main import create_app

    service = FakeChatService()
    app = create_app(chat_service=service)
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": "/api/v1/chat/completions",
            "headers": [],
        }
    )
    payload = ChatCompletionRequest.model_validate(
        {"message": {"content": "客户端立即断开"}}
    )

    async def exercise() -> None:
        response = await create_chat_completion(payload, request)

        assert response.background is not None
        await response.background()
        assert service.last_turn.active_run.cancel_reason == "disconnected"
        assert service.last_turn.active_run.terminal_future.done()
        assert service.touched_session_ids == [service.session_id]

    asyncio.run(exercise())


def test_delayed_response_background_does_not_cancel_replacement_run() -> None:
    from fastapi import Request

    from agent_runtime.api.routes.chat import create_chat_completion
    from agent_runtime.api.schemas.chat import ChatCompletionRequest
    from agent_runtime.main import create_app

    service = FakeChatService()
    app = create_app(chat_service=service)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat/completions",
            "headers": [],
            "app": app,
        }
    )

    async def exercise() -> None:
        response = await create_chat_completion(
            ChatCompletionRequest.model_validate(
                {"message": {"content": "旧请求"}}
            ),
            request,
        )
        stale_run = service.last_turn.active_run
        await service._run_registry.release(stale_run)
        replacement = await service._run_registry.reserve(
            service.session_id,
            UUID("00000000-0000-0000-0000-000000000804"),
        )

        assert response.background is not None
        await response.background()

        assert replacement.cancel_reason is None
        assert replacement.terminal_future.done() is False
        await service._run_registry.release(replacement)

    asyncio.run(exercise())


def test_chat_response_releases_run_when_asgi_send_disconnects() -> None:
    from starlette.requests import ClientDisconnect

    from agent_runtime.api.routes.chat import create_chat_completion
    from agent_runtime.api.schemas.chat import ChatCompletionRequest
    from agent_runtime.main import create_app

    service = FakeChatService()
    service.events = [AIMessageChunk(content="首段")]
    service.stream_blocker = asyncio.Event()
    app = create_app(chat_service=service)
    request = httpx.Request(
        "POST",
        "http://testserver/api/v1/chat/completions",
    )
    payload = ChatCompletionRequest.model_validate(
        {"message": {"content": "连接中断"}}
    )

    async def exercise() -> None:
        from fastapi import Request

        route_request = Request(
            {
                "type": "http",
                "asgi": {"spec_version": "2.4"},
                "app": app,
                "method": request.method,
                "path": request.url.path,
                "headers": [],
            }
        )
        response = await create_chat_completion(payload, route_request)

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("客户端已断开")

        with pytest.raises(ClientDisconnect):
            await response(route_request.scope, receive, send)

        assert service.last_turn.active_run.cancel_reason == "disconnected"
        assert service.last_turn.active_run.terminal_future.done()

    asyncio.run(exercise())
