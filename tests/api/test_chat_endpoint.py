"""Stage 1 聊天 HTTP + SSE 端点测试。"""

import asyncio
import json
from uuid import UUID

import httpx
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
        from agent_runtime.chat import PreparedChatTurn

        self.session_id = UUID("00000000-0000-0000-0000-000000000801")
        self.message_id = UUID("00000000-0000-0000-0000-000000000803")
        self.prepared = PreparedChatTurn(
            session_id=self.session_id,
            human_message=HumanMessage(
                content="测试消息",
                id="00000000-0000-0000-0000-000000000802",
            ),
            response_message_id=self.message_id,
            config={
                "configurable": {
                    "thread_id": str(self.session_id),
                    "message_id": str(self.message_id),
                }
            },
        )
        self.events: list[object] = []
        self.status = "completed"
        self.prepare_error: Exception | None = None
        self.prepare_calls: list[tuple[UUID | None, str]] = []
        self.incomplete_contents: list[str] = []
        self.persist_error: Exception | None = None

    async def prepare_turn(self, *, session_id, content):
        self.prepare_calls.append((session_id, content))
        if self.prepare_error is not None:
            raise self.prepare_error
        return self.prepared

    async def stream_turn(self, turn):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event

    async def get_completion_status(self, turn):
        return self.status

    async def persist_incomplete(self, turn, content):
        self.incomplete_contents.append(content)
        if self.persist_error is not None:
            raise self.persist_error


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


def test_chat_endpoint_rejects_invalid_request_before_service_call() -> None:
    from agent_runtime.main import create_app

    service = FakeChatService()
    app = create_app(chat_service=service)

    response = _post(app, {"message": {"content": "   "}})

    assert response.status_code == 422
    assert not response.headers["content-type"].startswith("text/event-stream")
    assert service.prepare_calls == []
