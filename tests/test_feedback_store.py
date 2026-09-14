"""S2-07 消息反馈服务及 PostgreSQL 存储测试。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import pytest


def test_feedback_store_setup_creates_constrained_table_and_cleanup_index(
    monkeypatch,
) -> None:
    from agent_runtime import feedback
    from agent_runtime.core.config import Settings

    statements: list[str] = []

    class FakeConnection:
        committed = False

        async def execute(self, query: str, params=None):
            statements.append(" ".join(query.split()))

        async def commit(self) -> None:
            self.committed = True

    connection = FakeConnection()

    @asynccontextmanager
    async def fake_connection_factory(settings):
        yield connection

    monkeypatch.setattr(
        feedback,
        "open_database_connection",
        fake_connection_factory,
    )
    store = feedback.PostgresFeedbackStore(Settings(_env_file=None))

    asyncio.run(store.setup())

    assert connection.committed is True
    assert len(statements) == 2
    assert "CREATE TABLE IF NOT EXISTS message_feedback" in statements[0]
    for definition in (
        "user_id TEXT NOT NULL",
        "session_id UUID NOT NULL",
        "message_id UUID NOT NULL",
        "value TEXT NOT NULL CHECK (value IN ('like', 'dislike'))",
        "updated_at TIMESTAMPTZ NOT NULL",
        "PRIMARY KEY (user_id, message_id)",
    ):
        assert definition in statements[0]
    assert statements[1] == (
        "CREATE INDEX IF NOT EXISTS message_feedback_session_id_idx "
        "ON message_feedback (session_id)"
    )


def test_feedback_store_upserts_one_final_value(monkeypatch) -> None:
    from agent_runtime import feedback
    from agent_runtime.core.config import Settings

    message_id = UUID("00000000-0000-0000-0000-000000002721")
    session_id = UUID("00000000-0000-0000-0000-000000002720")
    updated_at = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
    executed: dict[str, object] = {}

    class FakeConnection:
        committed = False

        async def execute(self, query: str, params=None):
            executed["query"] = " ".join(query.split())
            executed["params"] = params

        async def commit(self) -> None:
            self.committed = True

    connection = FakeConnection()

    @asynccontextmanager
    async def fake_connection_factory(settings):
        yield connection

    monkeypatch.setattr(
        feedback,
        "open_database_connection",
        fake_connection_factory,
    )
    store = feedback.PostgresFeedbackStore(Settings(_env_file=None))

    asyncio.run(
        store.upsert(
            user_id="configured-user",
            session_id=session_id,
            message_id=message_id,
            value="dislike",
            updated_at=updated_at,
        )
    )

    assert connection.committed is True
    assert executed["query"] == (
        "INSERT INTO message_feedback "
        "(user_id, session_id, message_id, value, updated_at) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (user_id, message_id) DO UPDATE SET "
        "session_id = EXCLUDED.session_id, value = EXCLUDED.value, "
        "updated_at = EXCLUDED.updated_at"
    )
    assert executed["params"] == (
        "configured-user",
        session_id,
        message_id,
        "dislike",
        updated_at,
    )


def test_feedback_store_cancel_is_idempotent_delete(monkeypatch) -> None:
    from agent_runtime import feedback
    from agent_runtime.core.config import Settings

    message_id = UUID("00000000-0000-0000-0000-000000002722")
    executed: dict[str, object] = {}

    class FakeConnection:
        committed = False

        async def execute(self, query: str, params=None):
            executed["query"] = " ".join(query.split())
            executed["params"] = params

        async def commit(self) -> None:
            self.committed = True

    connection = FakeConnection()

    @asynccontextmanager
    async def fake_connection_factory(settings):
        yield connection

    monkeypatch.setattr(
        feedback,
        "open_database_connection",
        fake_connection_factory,
    )
    store = feedback.PostgresFeedbackStore(Settings(_env_file=None))

    asyncio.run(
        store.delete(user_id="configured-user", message_id=message_id)
    )

    assert connection.committed is True
    assert executed["query"] == (
        "DELETE FROM message_feedback WHERE user_id = %s AND message_id = %s"
    )
    assert executed["params"] == ("configured-user", message_id)


def test_feedback_store_deletes_all_session_rows_for_fixed_user(
    monkeypatch,
) -> None:
    from agent_runtime import feedback
    from agent_runtime.core.config import Settings

    session_id = UUID("00000000-0000-0000-0000-000000002802")
    executed: dict[str, object] = {}

    class FakeConnection:
        committed = False

        async def execute(self, query: str, params=None):
            executed["query"] = " ".join(query.split())
            executed["params"] = params

        async def commit(self) -> None:
            self.committed = True

    connection = FakeConnection()

    @asynccontextmanager
    async def fake_connection_factory(settings):
        yield connection

    monkeypatch.setattr(
        feedback,
        "open_database_connection",
        fake_connection_factory,
    )
    store = feedback.PostgresFeedbackStore(Settings(_env_file=None))

    asyncio.run(
        store.delete_by_session(
            user_id="configured-user",
            session_id=session_id,
        )
    )

    assert connection.committed is True
    assert executed["query"] == (
        "DELETE FROM message_feedback WHERE user_id = %s AND session_id = %s"
    )
    assert executed["params"] == ("configured-user", session_id)


def test_feedback_store_bulk_reads_only_requested_user_and_messages(
    monkeypatch,
) -> None:
    from agent_runtime import feedback
    from agent_runtime.core.config import Settings

    first_id = UUID("00000000-0000-0000-0000-000000002723")
    second_id = UUID("00000000-0000-0000-0000-000000002724")
    executed: dict[str, object] = {}

    class FakeCursor:
        async def fetchall(self):
            return [
                {"message_id": first_id, "value": "like"},
                {"message_id": second_id, "value": "dislike"},
            ]

    class FakeConnection:
        async def execute(self, query: str, params=None):
            executed["query"] = " ".join(query.split())
            executed["params"] = params
            return FakeCursor()

    @asynccontextmanager
    async def fake_connection_factory(settings):
        yield FakeConnection()

    monkeypatch.setattr(
        feedback,
        "open_database_connection",
        fake_connection_factory,
    )
    store = feedback.PostgresFeedbackStore(Settings(_env_file=None))

    result = asyncio.run(
        store.list_for_messages(
            user_id="configured-user",
            message_ids=(first_id, second_id),
        )
    )

    assert result == {first_id: "like", second_id: "dislike"}
    assert executed["query"] == (
        "SELECT message_id, value FROM message_feedback "
        "WHERE user_id = %s AND message_id = ANY(%s::uuid[])"
    )
    assert executed["params"] == (
        "configured-user",
        [first_id, second_id],
    )


def test_feedback_store_skips_database_for_empty_message_list(monkeypatch) -> None:
    from agent_runtime import feedback
    from agent_runtime.core.config import Settings

    @asynccontextmanager
    async def unexpected_connection_factory(settings):
        raise AssertionError("空消息列表不应连接数据库")
        yield

    monkeypatch.setattr(
        feedback,
        "open_database_connection",
        unexpected_connection_factory,
    )
    store = feedback.PostgresFeedbackStore(Settings(_env_file=None))

    result = asyncio.run(
        store.list_for_messages(user_id="configured-user", message_ids=())
    )

    assert result == {}


def test_feedback_service_validates_target_then_saves_replaces_and_cancels() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.feedback import FeedbackService
    from agent_runtime.runs import ActiveRunRegistry

    message_id = UUID("00000000-0000-0000-0000-000000002725")
    session_id = UUID("00000000-0000-0000-0000-000000002726")
    now = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)

    class FakeTargetFinder:
        calls: list[UUID] = []

        async def find_feedback_session(self, *, message_id: UUID) -> UUID:
            self.calls.append(message_id)
            return session_id

    class FakeStore:
        operations: list[tuple] = []

        async def upsert(self, **kwargs) -> None:
            self.operations.append(("upsert", kwargs))

        async def delete(self, **kwargs) -> None:
            self.operations.append(("delete", kwargs))

    finder = FakeTargetFinder()
    store = FakeStore()
    service = FeedbackService(
        settings=Settings(_env_file=None, local_user_id="configured-user"),
        target_finder=finder,
        store=store,
        operation_coordinator=ActiveRunRegistry(),
        now_factory=lambda: now,
    )

    async def exercise():
        liked = await service.submit(message_id=message_id, action="like")
        disliked = await service.submit(
            message_id=message_id,
            action="dislike",
        )
        cancelled = await service.submit(
            message_id=message_id,
            action="cancel",
        )
        return liked, disliked, cancelled

    liked, disliked, cancelled = asyncio.run(exercise())

    assert liked.feedback == "like"
    assert disliked.feedback == "dislike"
    assert cancelled.feedback is None
    assert finder.calls == [message_id, message_id, message_id]
    assert store.operations == [
        (
            "upsert",
            {
                "user_id": "configured-user",
                "session_id": session_id,
                "message_id": message_id,
                "value": "like",
                "updated_at": now,
            },
        ),
        (
            "upsert",
            {
                "user_id": "configured-user",
                "session_id": session_id,
                "message_id": message_id,
                "value": "dislike",
                "updated_at": now,
            },
        ),
        (
            "delete",
            {
                "user_id": "configured-user",
                "message_id": message_id,
            },
        ),
    ]


def test_feedback_write_finishes_before_concurrent_session_deletion() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.feedback import FeedbackService
    from agent_runtime.runs import ActiveRunRegistry

    message_id = UUID("00000000-0000-0000-0000-000000002729")
    session_id = UUID("00000000-0000-0000-0000-000000002730")

    class FakeTargetFinder:
        async def find_feedback_session(self, *, message_id: UUID) -> UUID:
            return session_id

    class BlockingStore:
        def __init__(self) -> None:
            self.write_started = asyncio.Event()
            self.allow_write = asyncio.Event()

        async def upsert(self, **kwargs) -> None:
            self.write_started.set()
            await self.allow_write.wait()

        async def delete(self, **kwargs) -> None:
            raise AssertionError("本测试不应取消反馈")

    async def exercise() -> None:
        registry = ActiveRunRegistry()
        store = BlockingStore()
        service = FeedbackService(
            settings=Settings(_env_file=None),
            target_finder=FakeTargetFinder(),
            store=store,
            operation_coordinator=registry,
        )
        deletion_entered = asyncio.Event()

        feedback_task = asyncio.create_task(
            service.submit(message_id=message_id, action="like")
        )
        await store.write_started.wait()

        async def delete_session() -> None:
            async with registry.deleting(session_id):
                deletion_entered.set()

        deletion_task = asyncio.create_task(delete_session())
        await asyncio.sleep(0)
        assert deletion_entered.is_set() is False

        store.allow_write.set()
        result, _ = await asyncio.gather(feedback_task, deletion_task)
        assert result.feedback == "like"
        assert deletion_entered.is_set() is True

    asyncio.run(exercise())


def test_feedback_cannot_write_after_session_deletion_has_started() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.feedback import FeedbackService
    from agent_runtime.runs import ActiveRunRegistry, SessionUnavailableError

    message_id = UUID("00000000-0000-0000-0000-000000002731")
    session_id = UUID("00000000-0000-0000-0000-000000002732")

    class PausingTargetFinder:
        def __init__(self) -> None:
            self.validated = asyncio.Event()
            self.allow_return = asyncio.Event()

        async def find_feedback_session(self, *, message_id: UUID) -> UUID:
            self.validated.set()
            await self.allow_return.wait()
            return session_id

    class UnexpectedStore:
        async def upsert(self, **kwargs) -> None:
            raise AssertionError("删除开始后不得写入反馈")

        async def delete(self, **kwargs) -> None:
            raise AssertionError("删除开始后不得取消反馈")

    async def exercise() -> None:
        registry = ActiveRunRegistry()
        finder = PausingTargetFinder()
        service = FeedbackService(
            settings=Settings(_env_file=None),
            target_finder=finder,
            store=UnexpectedStore(),
            operation_coordinator=registry,
        )
        feedback_task = asyncio.create_task(
            service.submit(message_id=message_id, action="dislike")
        )
        await finder.validated.wait()

        async with registry.deleting(session_id) as deletion:
            finder.allow_return.set()
            with pytest.raises(SessionUnavailableError) as captured:
                await feedback_task
            assert captured.value.code == "SESSION_NOT_FOUND"
            deletion.mark_deleted()

    asyncio.run(exercise())


def test_feedback_store_maps_database_failure_to_stable_application_error(
    monkeypatch,
) -> None:
    from agent_runtime import feedback
    from agent_runtime.core.config import Settings

    @asynccontextmanager
    async def failing_connection_factory(settings):
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(
        feedback,
        "open_database_connection",
        failing_connection_factory,
    )
    store = feedback.PostgresFeedbackStore(Settings(_env_file=None))

    with pytest.raises(feedback.FeedbackError) as captured:
        asyncio.run(
            store.upsert(
                user_id="configured-user",
                session_id=UUID(
                    "00000000-0000-0000-0000-000000002727"
                ),
                message_id=UUID(
                    "00000000-0000-0000-0000-000000002728"
                ),
                value="like",
                updated_at=datetime.now(UTC),
            )
        )

    assert captured.value.code == "MESSAGE_FEEDBACK_PERSIST_FAILED"
    assert captured.value.message == "消息反馈保存失败"
    assert captured.value.retryable is True
