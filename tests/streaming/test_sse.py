import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessageChunk


@pytest.mark.parametrize(
    ("event_name", "payload_type", "payload", "expected"),
    [
        (
            "message",
            "MessageEventData",
            {
                "session_id": UUID("00000000-0000-0000-0000-000000000001"),
                "message_id": UUID("00000000-0000-0000-0000-000000000002"),
                "capability_id": "general_chat",
                "delta": "你好",
            },
            {
                "session_id": "00000000-0000-0000-0000-000000000001",
                "message_id": "00000000-0000-0000-0000-000000000002",
                "capability_id": "general_chat",
                "delta": "你好",
            },
        ),
        (
            "error",
            "ErrorEventData",
            {
                "session_id": UUID("00000000-0000-0000-0000-000000000001"),
                "code": "MODEL_CALL_FAILED",
                "message": "模型调用失败",
                "retryable": False,
            },
            {
                "session_id": "00000000-0000-0000-0000-000000000001",
                "code": "MODEL_CALL_FAILED",
                "message": "模型调用失败",
                "retryable": False,
            },
        ),
        (
            "done",
            "DoneEventData",
            {
                "session_id": UUID("00000000-0000-0000-0000-000000000001"),
                "message_id": UUID("00000000-0000-0000-0000-000000000002"),
                "capability_id": None,
                "status": "completed",
            },
            {
                "session_id": "00000000-0000-0000-0000-000000000001",
                "message_id": "00000000-0000-0000-0000-000000000002",
                "capability_id": None,
                "status": "completed",
            },
        ),
    ],
)
def test_encode_sse_emits_only_stable_product_protocol(
    event_name: str,
    payload_type: str,
    payload: dict[str, object],
    expected: dict[str, object],
) -> None:
    from agent_runtime.api.schemas import chat
    from agent_runtime.streaming.sse import encode_sse

    schema_type = getattr(chat, payload_type)

    encoded = encode_sse(event_name, schema_type.model_validate(payload))

    lines = encoded.splitlines()
    assert lines[0] == f"event: {event_name}"
    assert json.loads(lines[1].removeprefix("data: ")) == expected
    assert lines[2:] == [""]
    assert "node" not in encoded
    assert "checkpoint" not in encoded


def test_closing_sse_consumer_cancels_producer_and_releases_session() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.runs import ActiveRunRegistry
    from agent_runtime.sessions.models import Session
    from agent_runtime.streaming.sse import stream_chat_sse

    session_id = UUID("00000000-0000-0000-0000-000000001271")
    registry = ActiveRunRegistry()
    producer_cancelled = asyncio.Event()
    parent_updates: list[dict[str, object]] = []

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            now = datetime.now(UTC)
            return Session(
                session_id=requested_session_id,
                user_id="configured-user",
                title="会话",
                created_at=now,
                updated_at=now,
            )

    class BlockingParentGraph:
        async def aget_state(self, config):
            return SimpleNamespace(
                values={"messages": [], "completion_status": "completed"}
            )

        async def astream(self, state, config, *, stream_mode):
            try:
                yield AIMessageChunk(content="首段")
                await asyncio.Event().wait()
            finally:
                producer_cancelled.set()

        async def aupdate_state(self, config, values, *, as_node=None):
            parent_updates.append(values)

    class FakeSessionService:
        async def touch_session(self, *, session_id: UUID) -> None:
            pass

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=FakeSessionService(),
        parent_graph=BlockingParentGraph(),
        run_registry=registry,
    )

    async def exercise() -> None:
        turn = await service.prepare_turn(session_id=session_id, content="继续")
        stream = stream_chat_sse(service, turn)

        first_frame = await anext(stream)
        await stream.aclose()

        assert first_frame.startswith("event: message\n")
        assert producer_cancelled.is_set()
        assert turn.active_run.cancel_reason == "disconnected"
        incomplete_message = parent_updates[-1]["messages"][0]
        assert incomplete_message.content == "首段"
        assert incomplete_message.additional_kwargs["runtime_status"] == (
            "incomplete"
        )
        replacement = await registry.reserve(session_id, uuid4())
        await registry.release(replacement)

    asyncio.run(exercise())


def test_delayed_sse_cleanup_does_not_cancel_replacement_run() -> None:
    from langchain_core.messages import HumanMessage

    from agent_runtime.chat import PreparedChatTurn
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.runs import ActiveRunRegistry
    from agent_runtime.streaming.sse import stream_chat_sse

    session_id = UUID("00000000-0000-0000-0000-000000001275")
    response_message_id = UUID("00000000-0000-0000-0000-000000001276")
    registry = ActiveRunRegistry()

    class DelayedResponseService:
        async def start_producer(self, turn) -> None:
            pass

        async def cancel_run(self, run, *, reason):
            return await registry.request_cancel(run, reason)

    async def exercise() -> None:
        stale_run = await registry.reserve(session_id, response_message_id)
        turn = PreparedChatTurn(
            session_id=session_id,
            human_message=HumanMessage(content="旧请求"),
            response_message_id=response_message_id,
            config=parent_thread_config(session_id),
            active_run=stale_run,
        )
        stale_run.event_queue.put_nowait(None)
        await registry.release(stale_run)
        replacement = await registry.reserve(session_id, uuid4())

        stream = stream_chat_sse(DelayedResponseService(), turn)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

        assert replacement.cancel_reason is None
        assert replacement.terminal_future.done() is False
        await registry.release(replacement)

    asyncio.run(exercise())
