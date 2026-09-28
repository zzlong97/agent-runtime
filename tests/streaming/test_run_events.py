import asyncio
from datetime import UTC, datetime
from time import perf_counter
from uuid import UUID, uuid4

import pytest


RUN_ID = UUID("00000000-0000-0000-0000-000000002560")


def _public_event():
    from agent_runtime.runtime.event_models import RuntimeEvent
    from agent_runtime.runtime.event_schemas import project_public_event

    return project_public_event(
        RuntimeEvent(
            event_id=uuid4(),
            run_id=RUN_ID,
            seq=7,
            event_type="run.completed",
            source="executor",
            visibility="public",
            payload={"status": "completed"},
            schema_version=1,
            durability="durable",
            created_at=datetime(2026, 9, 27, 12, 0, tzinfo=UTC),
        )
    )


def test_last_event_id_takes_priority_over_after_seq() -> None:
    from agent_runtime.streaming.run_events import resolve_run_event_cursor

    assert resolve_run_event_cursor(last_event_id="9", after_seq=3) == 9
    assert resolve_run_event_cursor(last_event_id=None, after_seq=3) == 3


@pytest.mark.parametrize("value", ["", "-1", "1.5", "not-a-sequence"])
def test_invalid_last_event_id_is_rejected(value: str) -> None:
    from agent_runtime.core.errors import ApplicationError
    from agent_runtime.streaming.run_events import resolve_run_event_cursor

    with pytest.raises(ApplicationError) as captured:
        resolve_run_event_cursor(last_event_id=value, after_seq=3)

    assert captured.value.code == "RUN_EVENT_CURSOR_INVALID"
    assert captured.value.status_code == 400


def test_run_event_and_heartbeat_use_fixed_sse_frames() -> None:
    from agent_runtime.runtime.event_gateway import GatewayHeartbeat
    from agent_runtime.streaming.run_events import encode_run_event_sse

    event_frame = encode_run_event_sse(_public_event())

    assert event_frame.startswith("id: 7\nevent: run.completed\ndata: ")
    assert '"seq":7' in event_frame
    assert '"event_type":"run.completed"' in event_frame
    assert event_frame.endswith("\n\n")
    assert encode_run_event_sse(GatewayHeartbeat()) == ": heartbeat\n\n"


def test_slow_client_write_timeout_closes_only_stream_iterator() -> None:
    from agent_runtime.streaming.run_events import RunEventStreamingResponse

    closed = asyncio.Event()

    async def content():
        try:
            yield "id: 1\nevent: run.started\ndata: {}\n\n"
        finally:
            closed.set()

    async def send(message) -> None:
        if message["type"] == "http.response.body":
            await asyncio.sleep(1)

    async def exercise() -> float:
        response = RunEventStreamingResponse(
            content(),
            write_timeout_seconds=0.01,
        )
        started_at = perf_counter()
        await response.stream_response(send)
        await asyncio.wait_for(closed.wait(), timeout=0.2)
        return perf_counter() - started_at

    assert asyncio.run(exercise()) < 0.2


def test_slow_response_start_is_also_bounded_by_write_timeout() -> None:
    from agent_runtime.streaming.run_events import RunEventStreamingResponse

    class Content:
        def __init__(self) -> None:
            self.closed = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.closed.set()

    async def send(_message) -> None:
        await asyncio.sleep(1)

    async def exercise() -> float:
        content = Content()
        response = RunEventStreamingResponse(
            content,
            write_timeout_seconds=0.01,
        )
        started_at = perf_counter()
        await response.stream_response(send)
        await asyncio.wait_for(content.closed.wait(), timeout=0.2)
        return perf_counter() - started_at

    assert asyncio.run(exercise()) < 0.2


def test_sse_disconnect_closes_stream_without_cancelling_executor() -> None:
    """ASGI 断开只结束响应消费，不得传播取消到独立 Executor。"""

    from agent_runtime.streaming.run_events import (
        RunEventStreamingResponse,
        encode_run_event_stream,
    )

    stream_closed = asyncio.Event()
    executor_cancelled = False

    async def gateway_events():
        try:
            yield _public_event()
            await asyncio.Event().wait()
        finally:
            stream_closed.set()

    async def executor() -> str:
        nonlocal executor_cancelled
        try:
            await asyncio.sleep(0.02)
            return "completed"
        except asyncio.CancelledError:
            executor_cancelled = True
            raise

    async def exercise() -> None:
        first_body_sent = asyncio.Event()
        response = RunEventStreamingResponse(
            encode_run_event_stream(gateway_events()),
            run_id=RUN_ID,
        )
        executor_task = asyncio.create_task(executor())

        async def send(message) -> None:
            if (
                message["type"] == "http.response.body"
                and message.get("more_body")
            ):
                first_body_sent.set()

        async def receive():
            await first_body_sent.wait()
            return {"type": "http.disconnect"}

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/chat/runs/test/events",
            "raw_path": b"/api/v1/chat/runs/test/events",
            "query_string": b"",
            "headers": [],
            "client": ("test", 1),
            "server": ("test", 80),
            "root_path": "",
        }
        await response(scope, receive, send)
        await asyncio.wait_for(stream_closed.wait(), timeout=0.2)
        assert await asyncio.wait_for(executor_task, timeout=0.2) == "completed"

    asyncio.run(exercise())
    assert executor_cancelled is False


def test_full_app_slow_socket_is_bounded_by_sse_write_timeout(
    monkeypatch,
) -> None:
    """HTTP 日志中间件不得把 SSE 超时隔离在内存缓冲区内。"""

    from agent_runtime.api.routes import chat as chat_routes
    from agent_runtime.main import create_app
    from agent_runtime.streaming.run_events import RunEventStreamingResponse

    class FakeService:
        async def open_run_event_stream(self, *, run_id, after_seq):
            assert run_id == RUN_ID
            assert after_seq == 0

            async def events():
                yield _public_event()

            return events()

    def response_with_short_timeout(content, *, run_id):
        return RunEventStreamingResponse(
            content,
            run_id=run_id,
            write_timeout_seconds=0.01,
        )

    monkeypatch.setattr(
        chat_routes,
        "RunEventStreamingResponse",
        response_with_short_timeout,
    )
    app = create_app(chat_service=FakeService())

    async def exercise() -> float:
        request_delivered = False

        async def receive():
            nonlocal request_delivered
            if not request_delivered:
                request_delivered = True
                return {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                }
            await asyncio.Event().wait()

        async def slow_socket_send(message) -> None:
            if message["type"] == "http.response.body":
                await asyncio.sleep(1)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/api/v1/chat/runs/{RUN_ID}/events",
            "raw_path": (
                f"/api/v1/chat/runs/{RUN_ID}/events".encode("ascii")
            ),
            "query_string": b"after_seq=0",
            "headers": [],
            "client": ("test", 1),
            "server": ("test", 80),
            "root_path": "",
        }
        started_at = perf_counter()
        await asyncio.wait_for(
            app(scope, receive, slow_socket_send),
            timeout=0.2,
        )
        return perf_counter() - started_at

    assert asyncio.run(exercise()) < 0.2
