"""S2-08 Session 持久化关联数据删除顺序测试。"""

import asyncio
from uuid import UUID

import pytest


SESSION_ID = UUID("00000000-0000-0000-0000-000000002801")


class RecordingCheckpointer:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.fail_once = False

    async def adelete_thread(self, thread_id: str) -> None:
        self.events.append(f"{self.name}:{thread_id}")
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError(f"{self.name} 暂不可用")


class RecordingFeedbackStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.fail_once = False

    async def delete_by_session(self, *, user_id: str, session_id: UUID) -> None:
        self.events.append(f"feedback:{user_id}:{session_id}")
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("反馈存储暂不可用")


class RecordingSessionRepository:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.fail_once = False
        self.deleted = False

    async def delete_owned(self, *, session_id: UUID, user_id: str) -> None:
        self.events.append(f"session:{user_id}:{session_id}")
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("Session 存储暂不可用")
        self.deleted = True


def _deletion_service(events: list[str]):
    from agent_runtime.core.config import Settings
    from agent_runtime.session_deletion import SessionDeletionService

    parent = RecordingCheckpointer("parent", events)
    general_chat = RecordingCheckpointer("general_chat", events)
    en_to_zh = RecordingCheckpointer("en_to_zh", events)
    feedback = RecordingFeedbackStore(events)
    sessions = RecordingSessionRepository(events)
    service = SessionDeletionService(
        settings=Settings(
            _env_file=None,
            local_user_id="configured-user",
        ),
        parent_checkpointer=parent,
        general_chat_checkpointer=general_chat,
        en_to_zh_checkpointer=en_to_zh,
        feedback_store=feedback,
        session_repository=sessions,
    )
    return service, parent, general_chat, en_to_zh, feedback, sessions


def test_session_deletion_service_deletes_in_fixed_retry_safe_order() -> None:
    events: list[str] = []
    service, _parent, _general, _translation, _feedback, sessions = (
        _deletion_service(events)
    )

    asyncio.run(service.delete_persisted_data(session_id=SESSION_ID))

    assert events == [
        f"general_chat:{SESSION_ID}:general_chat",
        f"en_to_zh:{SESSION_ID}:en_to_zh",
        f"parent:{SESSION_ID}",
        f"feedback:configured-user:{SESSION_ID}",
        f"session:configured-user:{SESSION_ID}",
    ]
    assert sessions.deleted is True


@pytest.mark.parametrize(
    "failing_dependency",
    ["general_chat", "en_to_zh", "parent", "feedback", "session"],
)
def test_session_deletion_service_keeps_retry_anchor_and_retries_from_start(
    failing_dependency: str,
) -> None:
    from agent_runtime.session_deletion import SessionDeletionError

    events: list[str] = []
    service, parent, general_chat, en_to_zh, feedback, sessions = (
        _deletion_service(events)
    )
    dependencies = {
        "general_chat": general_chat,
        "en_to_zh": en_to_zh,
        "parent": parent,
        "feedback": feedback,
        "session": sessions,
    }
    dependencies[failing_dependency].fail_once = True

    with pytest.raises(SessionDeletionError) as captured:
        asyncio.run(service.delete_persisted_data(session_id=SESSION_ID))

    assert captured.value.code == "SESSION_DELETE_FAILED"
    assert captured.value.message == "Session 关联数据删除失败，请重试"
    assert captured.value.status_code == 500
    assert captured.value.retryable is True
    assert sessions.deleted is False

    events.clear()
    asyncio.run(service.delete_persisted_data(session_id=SESSION_ID))

    assert events == [
        f"general_chat:{SESSION_ID}:general_chat",
        f"en_to_zh:{SESSION_ID}:en_to_zh",
        f"parent:{SESSION_ID}",
        f"feedback:configured-user:{SESSION_ID}",
        f"session:configured-user:{SESSION_ID}",
    ]
    assert sessions.deleted is True
