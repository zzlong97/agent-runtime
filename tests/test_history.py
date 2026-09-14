"""活动 Parent 分支历史 Adapter 测试。"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


def _session(session_id: UUID, *, user_id: str = "configured-user"):
    from agent_runtime.sessions.models import Session

    now = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
    return Session(
        session_id=session_id,
        user_id=user_id,
        title="历史测试",
        created_at=now,
        updated_at=now,
    )


class FakeSessionRepository:
    def __init__(self, session) -> None:
        self.session = session
        self.calls: list[UUID] = []

    async def get(self, session_id: UUID):
        self.calls.append(session_id)
        return self.session


class FakeParentStateStore:
    def __init__(self, messages) -> None:
        self.messages = list(messages)
        self.calls: list[UUID] = []

    async def get_messages(self, session_id: UUID):
        self.calls.append(session_id)
        return list(self.messages)


def test_history_adapter_converts_product_messages_and_stage_one_metadata() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.history import MessageHistoryAdapter

    session_id = UUID("00000000-0000-0000-0000-000000001401")
    message_ids = [
        UUID(f"00000000-0000-0000-0000-{value:012d}")
        for value in range(1401, 1407)
    ]
    messages = [
        SystemMessage(content="内部系统提示", id=str(message_ids[0])),
        HumanMessage(content="旧用户消息", id=str(message_ids[1])),
        AIMessage(content="旧完成消息", id=str(message_ids[2])),
        AIMessage(
            content="不支持消息",
            id=str(message_ids[3]),
            additional_kwargs={
                "runtime_status": "unsupported",
                "capability_id": None,
            },
        ),
        AIMessage(
            content="不完整消息",
            id=str(message_ids[4]),
            additional_kwargs={
                "runtime_status": "incomplete",
                "capability_id": "general_chat",
            },
        ),
        AIMessage(
            content="停止消息",
            id=str(message_ids[5]),
            additional_kwargs={
                "runtime_status": "stopped",
                "capability_id": "en_to_zh",
            },
        ),
    ]
    repository = FakeSessionRepository(_session(session_id))
    state_store = FakeParentStateStore(messages)
    adapter = MessageHistoryAdapter(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=repository,
        parent_state_store=state_store,
    )

    page = asyncio.run(
        adapter.list_messages(session_id=session_id, before=None, limit=50)
    )

    assert repository.calls == [session_id]
    assert state_store.calls == [session_id]
    assert page.next_before is None
    assert [item.message_id for item in page.items] == message_ids[1:]
    assert [item.role for item in page.items] == [
        "user",
        "assistant",
        "assistant",
        "assistant",
        "assistant",
    ]
    assert [item.content for item in page.items] == [
        "旧用户消息",
        "旧完成消息",
        "不支持消息",
        "不完整消息",
        "停止消息",
    ]
    assert [item.runtime_status for item in page.items] == [
        None,
        "completed",
        "unsupported",
        "incomplete",
        "stopped",
    ]
    assert [item.capability_id for item in page.items] == [
        None,
        None,
        None,
        "general_chat",
        "en_to_zh",
    ]
    assert [item.feedback for item in page.items] == [None] * 5


def test_history_adapter_pages_backwards_and_keeps_each_page_chronological() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.history import MessageHistoryAdapter

    session_id = UUID("00000000-0000-0000-0000-000000001420")
    message_ids = [
        UUID(f"00000000-0000-0000-0000-{value:012d}")
        for value in range(1421, 1427)
    ]
    messages = [
        HumanMessage(content=f"消息 {index}", id=str(message_id))
        for index, message_id in enumerate(message_ids, start=1)
    ]
    adapter = MessageHistoryAdapter(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(_session(session_id)),
        parent_state_store=FakeParentStateStore(messages),
    )

    latest = asyncio.run(
        adapter.list_messages(session_id=session_id, before=None, limit=2)
    )
    previous = asyncio.run(
        adapter.list_messages(
            session_id=session_id,
            before=latest.next_before,
            limit=2,
        )
    )
    oldest = asyncio.run(
        adapter.list_messages(
            session_id=session_id,
            before=previous.next_before,
            limit=2,
        )
    )

    assert [item.message_id for item in latest.items] == message_ids[4:]
    assert latest.next_before == message_ids[4]
    assert [item.message_id for item in previous.items] == message_ids[2:4]
    assert previous.next_before == message_ids[2]
    assert [item.message_id for item in oldest.items] == message_ids[:2]
    assert oldest.next_before is None


def test_history_adapter_returns_complete_active_product_history() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.history import MessageHistoryAdapter

    session_id = UUID("00000000-0000-0000-0000-000000001425")
    messages = [
        HumanMessage(
            content=f"消息 {index}",
            id=f"00000000-0000-0000-0000-{index:012d}",
        )
        for index in range(1, 102)
    ]
    adapter = MessageHistoryAdapter(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(_session(session_id)),
        parent_state_store=FakeParentStateStore(messages),
    )

    active_messages = asyncio.run(
        adapter.get_active_messages(session_id=session_id)
    )

    assert len(active_messages) == 101
    assert [message.message_id for message in active_messages] == [
        UUID(str(message.id)) for message in messages
    ]


def test_history_adapter_rejects_before_outside_active_product_history() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.history import MessageHistoryAdapter, MessageHistoryError

    session_id = UUID("00000000-0000-0000-0000-000000001430")
    adapter = MessageHistoryAdapter(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(_session(session_id)),
        parent_state_store=FakeParentStateStore(
            [
                HumanMessage(
                    content="当前活动消息",
                    id="00000000-0000-0000-0000-000000001431",
                )
            ]
        ),
    )

    with pytest.raises(MessageHistoryError) as captured:
        asyncio.run(
            adapter.list_messages(
                session_id=session_id,
                before=UUID("00000000-0000-0000-0000-000000001499"),
                limit=50,
            )
        )

    assert captured.value.code == "MESSAGE_BEFORE_INVALID"
    assert captured.value.message == "before 不是当前活动历史中的消息"
    assert captured.value.status_code == 400


def test_history_adapter_hides_session_owned_by_another_user() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.history import MessageHistoryAdapter, MessageHistoryError

    session_id = UUID("00000000-0000-0000-0000-000000001440")
    state_store = FakeParentStateStore([])
    adapter = MessageHistoryAdapter(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(
            _session(session_id, user_id="another-user")
        ),
        parent_state_store=state_store,
    )

    with pytest.raises(MessageHistoryError) as captured:
        asyncio.run(
            adapter.list_messages(session_id=session_id, before=None, limit=50)
        )

    assert captured.value.code == "SESSION_NOT_FOUND"
    assert captured.value.status_code == 404
    assert state_store.calls == []


@pytest.mark.parametrize(
    ("message", "code"),
    [
        (HumanMessage(content="缺少 ID"), "MESSAGE_HISTORY_INVALID"),
        (
            AIMessage(
                content="非法状态",
                id="00000000-0000-0000-0000-000000001451",
                additional_kwargs={"runtime_status": "failed"},
            ),
            "MESSAGE_HISTORY_INVALID",
        ),
    ],
)
def test_history_adapter_rejects_corrupt_public_message_metadata(
    message,
    code: str,
) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.history import MessageHistoryAdapter, MessageHistoryError

    session_id = UUID("00000000-0000-0000-0000-000000001450")
    adapter = MessageHistoryAdapter(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=FakeSessionRepository(_session(session_id)),
        parent_state_store=FakeParentStateStore([message]),
    )

    with pytest.raises(MessageHistoryError) as captured:
        asyncio.run(
            adapter.list_messages(session_id=session_id, before=None, limit=50)
        )

    assert captured.value.code == code
    assert captured.value.status_code == 500
