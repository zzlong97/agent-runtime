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


class RecordingRuntimeStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.run_ids = [
            UUID("00000000-0000-0000-0000-000000002802"),
            UUID("00000000-0000-0000-0000-000000002803"),
        ]
        self.list_fail_once = False
        self.delete_fail_once = False

    async def list_run_ids_by_session(self, *, session_id: UUID):
        self.events.append(f"runtime-list:{session_id}")
        if self.list_fail_once:
            self.list_fail_once = False
            raise RuntimeError("Runtime 标识暂不可用")
        return self.run_ids

    async def delete_by_session(self, *, session_id: UUID) -> None:
        self.events.append(f"runtime-delete:{session_id}")
        if self.delete_fail_once:
            self.delete_fail_once = False
            raise RuntimeError("Runtime 存储暂不可用")


class RecordingCapabilityDataDeleter:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.task_ids = (
            UUID("00000000-0000-0000-0000-000000002804"),
            UUID("00000000-0000-0000-0000-000000002805"),
        )
        self.list_fail_once = False
        self.delete_fail_once = False

    async def list_task_ids_by_session(self, *, session_id: UUID):
        self.events.append(f"capability-task-list:{session_id}")
        if self.list_fail_once:
            self.list_fail_once = False
            raise RuntimeError("Capability Task 标识暂不可用")
        return self.task_ids

    async def delete_by_session(self, *, session_id: UUID) -> None:
        self.events.append(f"capability-delete:{session_id}")
        if self.delete_fail_once:
            self.delete_fail_once = False
            raise RuntimeError("Capability 数据暂不可用")


class RecordingChildThreadCollector:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.thread_ids = (
            f"capability:v1:{SESSION_ID}:alpha:schema:v1:run:"
            "00000000-0000-0000-0000-000000002802",
            f"{SESSION_ID}:legacy_agent",
        )
        self.fail_once = False

    async def list_thread_ids_by_session(self, *, session_id: UUID):
        self.events.append(f"child-thread-list:{session_id}")
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("Child thread 标识暂不可用")
        return self.thread_ids


class RecordingRedisCleaner:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.fail = False
        self.raise_once = False

    async def delete_streams(self, run_ids) -> bool:
        self.events.append(
            "redis:" + ",".join(str(run_id) for run_id in run_ids)
        )
        if self.raise_once:
            self.raise_once = False
            raise RuntimeError("Redis 暂不可用")
        return not self.fail


def _deletion_service(events: list[str]):
    from agent_runtime.core.config import Settings
    from agent_runtime.session_deletion import SessionDeletionService

    parent = RecordingCheckpointer("parent", events)
    child = RecordingCheckpointer("child", events)
    feedback = RecordingFeedbackStore(events)
    sessions = RecordingSessionRepository(events)
    runtime = RecordingRuntimeStore(events)
    capability_data = RecordingCapabilityDataDeleter(events)
    child_threads = RecordingChildThreadCollector(events)
    redis = RecordingRedisCleaner(events)
    service = SessionDeletionService(
        settings=Settings(
            _env_file=None,
            local_user_id="configured-user",
        ),
        parent_checkpointer=parent,
        child_checkpointer=child,
        feedback_store=feedback,
        session_repository=sessions,
        runtime_store=runtime,
        capability_data_deleter=capability_data,
        child_thread_collector=child_threads,
        redis_stream_cleaner=redis,
    )
    return (
        service,
        parent,
        child,
        feedback,
        sessions,
        runtime,
        capability_data,
        child_threads,
        redis,
    )


def test_session_deletion_service_deletes_in_fixed_retry_safe_order() -> None:
    events: list[str] = []
    (
        service,
        _parent,
        _child,
        _feedback,
        sessions,
        runtime,
        capability_data,
        child_threads,
        _redis,
    ) = (
        _deletion_service(events)
    )

    asyncio.run(service.delete_persisted_data(session_id=SESSION_ID))

    assert events == [
        f"runtime-list:{SESSION_ID}",
        f"capability-task-list:{SESSION_ID}",
        f"child-thread-list:{SESSION_ID}",
        "redis:" + ",".join(str(run_id) for run_id in runtime.run_ids),
        f"capability-delete:{SESSION_ID}",
        f"runtime-delete:{SESSION_ID}",
        *(
            f"child:{thread_id}"
            for thread_id in child_threads.thread_ids
        ),
        f"parent:{SESSION_ID}",
        f"feedback:configured-user:{SESSION_ID}",
        f"session:configured-user:{SESSION_ID}",
    ]
    assert sessions.deleted is True


@pytest.mark.parametrize(
    "failing_dependency",
    [
        "runtime_collect",
        "capability_collect",
        "thread_collect",
        "capability_delete",
        "runtime_delete",
        "child",
        "parent",
        "feedback",
        "session",
    ],
)
def test_session_deletion_service_keeps_retry_anchor_and_retries_from_start(
    failing_dependency: str,
) -> None:
    from agent_runtime.session_deletion import SessionDeletionError

    events: list[str] = []
    (
        service,
        parent,
        child,
        feedback,
        sessions,
        runtime,
        capability_data,
        child_threads,
        _redis,
    ) = (
        _deletion_service(events)
    )
    failure_flags = {
        "runtime_collect": (runtime, "list_fail_once"),
        "capability_collect": (capability_data, "list_fail_once"),
        "thread_collect": (child_threads, "fail_once"),
        "capability_delete": (capability_data, "delete_fail_once"),
        "runtime_delete": (runtime, "delete_fail_once"),
        "child": (child, "fail_once"),
        "parent": (parent, "fail_once"),
        "feedback": (feedback, "fail_once"),
        "session": (sessions, "fail_once"),
    }
    dependency, flag_name = failure_flags[failing_dependency]
    setattr(dependency, flag_name, True)

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
        f"runtime-list:{SESSION_ID}",
        f"capability-task-list:{SESSION_ID}",
        f"child-thread-list:{SESSION_ID}",
        "redis:" + ",".join(str(run_id) for run_id in runtime.run_ids),
        f"capability-delete:{SESSION_ID}",
        f"runtime-delete:{SESSION_ID}",
        *(
            f"child:{thread_id}"
            for thread_id in child_threads.thread_ids
        ),
        f"parent:{SESSION_ID}",
        f"feedback:configured-user:{SESSION_ID}",
        f"session:configured-user:{SESSION_ID}",
    ]
    assert sessions.deleted is True


def test_session_deletion_ignores_redis_cleanup_failure() -> None:
    events: list[str] = []
    (
        service,
        _parent,
        _child,
        _feedback,
        sessions,
        runtime,
        _capability_data,
        _child_threads,
        redis,
    ) = _deletion_service(events)
    redis.fail = True

    asyncio.run(service.delete_persisted_data(session_id=SESSION_ID))

    assert f"runtime-delete:{SESSION_ID}" in events
    assert sessions.deleted is True


def test_session_deletion_ignores_redis_cleanup_exception() -> None:
    events: list[str] = []
    (
        service,
        _parent,
        _child,
        _feedback,
        sessions,
        runtime,
        _capability_data,
        _child_threads,
        redis,
    ) = _deletion_service(events)
    redis.raise_once = True

    asyncio.run(service.delete_persisted_data(session_id=SESSION_ID))

    assert f"runtime-delete:{SESSION_ID}" in events
    assert sessions.deleted is True
