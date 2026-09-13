import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage


def _session(session_id: UUID, *, user_id: str = "configured-user"):
    from agent_runtime.sessions.models import Session

    now = datetime.now(UTC)
    return Session(
        session_id=session_id,
        user_id=user_id,
        title="首条消息",
        created_at=now,
        updated_at=now,
    )


class FakeParentGraph:
    def __init__(self) -> None:
        self.stream_events: list[object] = []
        self.completion_status = "completed"
        self.state_exists = True
        self.stream_calls: list[tuple[dict[str, object], dict[str, object], str]] = []
        self.updates: list[
            tuple[dict[str, object], dict[str, object], str | None]
        ] = []

    async def astream(self, state, config, *, stream_mode):
        self.stream_calls.append((state, config, stream_mode))
        for event in self.stream_events:
            if isinstance(event, Exception):
                raise event
            yield event

    async def aget_state(self, config):
        values = (
            {"messages": [], "completion_status": self.completion_status}
            if self.state_exists
            else {}
        )
        return SimpleNamespace(values=values)

    async def aupdate_state(self, config, values, *, as_node=None):
        self.updates.append((config, values, as_node))


def test_prepare_new_turn_uses_persisted_session_and_stable_ids() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions.service import SessionStart

    session_id = UUID("00000000-0000-0000-0000-000000000701")
    human_message = HumanMessage(
        content="新会话消息",
        id="00000000-0000-0000-0000-000000000702",
    )
    started = SessionStart(
        session=_session(session_id),
        human_message=human_message,
    )

    class FakeSessionService:
        async def prepare_new_session(self, *, content: str):
            assert content == "新会话消息"
            return started

    class UnexpectedRepository:
        async def get(self, session_id):
            raise AssertionError("新会话不应查询已有 Session")

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=UnexpectedRepository(),
        session_service=FakeSessionService(),
        parent_graph=FakeParentGraph(),
        response_message_id_factory=lambda: UUID(
            "00000000-0000-0000-0000-000000000703"
        ),
    )

    prepared = asyncio.run(service.prepare_turn(session_id=None, content="新会话消息"))

    assert prepared.session_id == session_id
    assert prepared.human_message == human_message
    assert prepared.response_message_id == UUID(
        "00000000-0000-0000-0000-000000000703"
    )
    assert prepared.config == {
        "configurable": {
            "thread_id": str(session_id),
            "message_id": "00000000-0000-0000-0000-000000000703",
        }
    }


def test_prepare_existing_turn_validates_session_and_parent_state() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings

    session_id = UUID("00000000-0000-0000-0000-000000000711")
    parent = FakeParentGraph()

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            assert requested_session_id == session_id
            return _session(session_id)

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=object(),
        parent_graph=parent,
        human_message_id_factory=lambda: UUID(
            "00000000-0000-0000-0000-000000000712"
        ),
        response_message_id_factory=lambda: UUID(
            "00000000-0000-0000-0000-000000000713"
        ),
    )

    prepared = asyncio.run(
        service.prepare_turn(session_id=session_id, content="继续会话")
    )

    assert prepared.human_message.content == "继续会话"
    assert prepared.human_message.id == "00000000-0000-0000-0000-000000000712"


@pytest.mark.parametrize(
    ("user_id", "state_exists", "expected_code", "expected_status"),
    [
        ("other-user", True, "SESSION_NOT_FOUND", 404),
        ("configured-user", False, "SESSION_STATE_NOT_FOUND", 409),
    ],
)
def test_prepare_existing_turn_rejects_invalid_session_before_streaming(
    user_id: str,
    state_exists: bool,
    expected_code: str,
    expected_status: int,
) -> None:
    from agent_runtime.chat import ChatService, ChatSessionError
    from agent_runtime.core.config import Settings

    session_id = uuid4()
    parent = FakeParentGraph()
    parent.state_exists = state_exists

    class FakeSessionRepository:
        async def get(self, requested_session_id):
            return _session(session_id, user_id=user_id)

    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(),
        session_service=object(),
        parent_graph=parent,
    )

    with pytest.raises(ChatSessionError) as captured:
        asyncio.run(service.prepare_turn(session_id=session_id, content="继续会话"))

    assert captured.value.code == expected_code
    assert captured.value.status_code == expected_status


def test_stream_turn_and_persist_incomplete_message_in_parent() -> None:
    from agent_runtime.chat import ChatService, PreparedChatTurn
    from agent_runtime.core.config import Settings

    session_id = UUID("00000000-0000-0000-0000-000000000721")
    response_message_id = UUID("00000000-0000-0000-0000-000000000723")
    config = {
        "configurable": {
            "thread_id": str(session_id),
            "message_id": str(response_message_id),
        }
    }
    turn = PreparedChatTurn(
        session_id=session_id,
        human_message=HumanMessage(
            content="触发部分输出",
            id="00000000-0000-0000-0000-000000000722",
        ),
        response_message_id=response_message_id,
        config=config,
    )
    parent = FakeParentGraph()
    parent.stream_events = [
        AIMessageChunk(content="部分"),
        AIMessageChunk(content="输出"),
    ]
    service = ChatService(
        settings=Settings(_env_file=None),
        session_repository=object(),
        session_service=object(),
        parent_graph=parent,
    )

    async def exercise():
        messages = [message async for message in service.stream_turn(turn)]
        status = await service.get_completion_status(turn)
        await service.persist_incomplete(turn, "部分输出")
        return messages, status

    messages, status = asyncio.run(exercise())

    assert [str(message.text) for message in messages] == ["部分", "输出"]
    assert status == "completed"
    assert parent.stream_calls == [
        ({"messages": [turn.human_message]}, config, "custom")
    ]
    assert len(parent.updates) == 1
    update_config, update_values, as_node = parent.updates[0]
    assert update_config == config
    assert as_node == "invoke_capability"
    incomplete_message = update_values["messages"][0]
    assert isinstance(incomplete_message, AIMessage)
    assert incomplete_message.content == "部分输出"
    assert incomplete_message.id == str(response_message_id)
    assert incomplete_message.additional_kwargs["runtime_status"] == "incomplete"


def test_chat_service_delegates_session_listing_to_session_service() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings

    expected_page = object()
    calls: list[tuple[str | None, int]] = []

    class FakeSessionService:
        async def list_sessions(self, *, cursor, limit):
            calls.append((cursor, limit))
            return expected_page

    service = ChatService(
        settings=Settings(_env_file=None),
        session_repository=object(),
        session_service=FakeSessionService(),
        parent_graph=FakeParentGraph(),
    )

    page = asyncio.run(service.list_sessions(cursor="opaque", limit=7))

    assert page is expected_page
    assert calls == [("opaque", 7)]


def test_chat_service_delegates_session_rename_to_session_service() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings

    session_id = UUID("00000000-0000-0000-0000-000000001101")
    expected_session = object()
    calls: list[tuple[UUID, str]] = []

    class FakeSessionService:
        async def rename_session(self, *, session_id, title):
            calls.append((session_id, title))
            return expected_session

    service = ChatService(
        settings=Settings(_env_file=None),
        session_repository=object(),
        session_service=FakeSessionService(),
        parent_graph=FakeParentGraph(),
    )

    renamed = asyncio.run(
        service.rename_session(session_id=session_id, title="新标题")
    )

    assert renamed is expected_session
    assert calls == [(session_id, "新标题")]
