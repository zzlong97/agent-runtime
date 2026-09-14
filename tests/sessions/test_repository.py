import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest


def test_session_repository_setup_creates_minimal_table_and_commits(
    monkeypatch,
) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions import repository

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
        repository,
        "open_database_connection",
        fake_connection_factory,
    )
    session_repository = repository.PostgresSessionRepository(
        Settings(_env_file=None)
    )

    asyncio.run(session_repository.setup())

    assert connection.committed is True
    assert len(statements) == 2
    assert "CREATE TABLE IF NOT EXISTS sessions" in statements[0]
    for column in (
        "session_id UUID PRIMARY KEY",
        "user_id TEXT NOT NULL",
        "title TEXT NOT NULL",
        "created_at TIMESTAMPTZ NOT NULL",
        "updated_at TIMESTAMPTZ NOT NULL",
    ):
        assert column in statements[0]
    assert statements[1] == (
        "CREATE INDEX IF NOT EXISTS sessions_user_updated_id_idx "
        "ON sessions (user_id, updated_at DESC, session_id DESC)"
    )


def test_session_repository_add_persists_all_session_fields(
    monkeypatch,
) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions import repository
    from agent_runtime.sessions.models import Session

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
        repository,
        "open_database_connection",
        fake_connection_factory,
    )
    session_repository = repository.PostgresSessionRepository(
        Settings(_env_file=None)
    )
    now = datetime.now(UTC)
    session = Session(
        session_id=uuid4(),
        user_id="configured-user",
        title="First message",
        created_at=now,
        updated_at=now,
    )

    asyncio.run(session_repository.add(session))

    assert connection.committed is True
    assert executed["query"] == (
        "INSERT INTO sessions "
        "(session_id, user_id, title, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s)"
    )
    assert executed["params"] == (
        session.session_id,
        session.user_id,
        session.title,
        session.created_at,
        session.updated_at,
    )


def test_session_repository_get_restores_session_metadata(
    monkeypatch,
) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions import repository
    from agent_runtime.sessions.models import Session

    session_id = uuid4()
    now = datetime.now(UTC)
    row = {
        "session_id": session_id,
        "user_id": "configured-user",
        "title": "First message",
        "created_at": now,
        "updated_at": now,
    }
    executed: dict[str, object] = {}

    class FakeCursor:
        async def fetchone(self):
            return row

    class FakeConnection:
        async def execute(self, query: str, params=None):
            executed["query"] = " ".join(query.split())
            executed["params"] = params
            return FakeCursor()

    @asynccontextmanager
    async def fake_connection_factory(settings):
        yield FakeConnection()

    monkeypatch.setattr(
        repository,
        "open_database_connection",
        fake_connection_factory,
    )
    session_repository = repository.PostgresSessionRepository(
        Settings(_env_file=None)
    )

    restored = asyncio.run(session_repository.get(session_id))

    assert restored == Session(**row)
    assert executed["query"] == (
        "SELECT session_id, user_id, title, created_at, updated_at "
        "FROM sessions WHERE session_id = %s"
    )
    assert executed["params"] == (session_id,)


def test_session_repository_get_reports_missing_session(monkeypatch) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions import repository

    class FakeCursor:
        async def fetchone(self):
            return None

    class FakeConnection:
        async def execute(self, query: str, params=None):
            return FakeCursor()

    @asynccontextmanager
    async def fake_connection_factory(settings):
        yield FakeConnection()

    monkeypatch.setattr(
        repository,
        "open_database_connection",
        fake_connection_factory,
    )
    session_repository = repository.PostgresSessionRepository(
        Settings(_env_file=None)
    )

    with pytest.raises(repository.SessionNotFoundError) as captured:
        asyncio.run(session_repository.get(uuid4()))

    assert captured.value.code == "SESSION_NOT_FOUND"
    assert captured.value.message == "Session 不存在"
    assert captured.value.status_code == 404


def test_session_repository_deletes_owned_session_last_and_idempotently(
    monkeypatch,
) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions import repository

    session_id = uuid4()
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
        repository,
        "open_database_connection",
        fake_connection_factory,
    )
    session_repository = repository.PostgresSessionRepository(
        Settings(_env_file=None)
    )

    asyncio.run(
        session_repository.delete_owned(
            session_id=session_id,
            user_id="configured-user",
        )
    )

    assert connection.committed is True
    assert executed["query"] == (
        "DELETE FROM sessions WHERE session_id = %s AND user_id = %s"
    )
    assert executed["params"] == (session_id, "configured-user")


def test_session_repository_lists_owned_ids_for_feedback_lookup(monkeypatch) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions import repository

    session_ids = [uuid4(), uuid4()]
    executed: dict[str, object] = {}

    class FakeCursor:
        async def fetchall(self):
            return [{"session_id": session_id} for session_id in session_ids]

    class FakeConnection:
        async def execute(self, query: str, params=None):
            executed["query"] = " ".join(query.split())
            executed["params"] = params
            return FakeCursor()

    @asynccontextmanager
    async def fake_connection_factory(settings):
        yield FakeConnection()

    monkeypatch.setattr(
        repository,
        "open_database_connection",
        fake_connection_factory,
    )
    session_repository = repository.PostgresSessionRepository(
        Settings(_env_file=None)
    )

    result = asyncio.run(
        session_repository.list_ids_by_user(user_id="configured-user")
    )

    assert result == session_ids
    assert executed["query"] == (
        "SELECT session_id FROM sessions WHERE user_id = %s"
    )
    assert executed["params"] == ("configured-user",)


def test_session_repository_lists_first_page_with_stable_order_and_overfetch(
    monkeypatch,
) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions import repository
    from agent_runtime.sessions.models import Session

    now = datetime.now(UTC)
    rows = [
        {
            "session_id": uuid4(),
            "user_id": "configured-user",
            "title": f"Session {index}",
            "created_at": now,
            "updated_at": now,
        }
        for index in range(3)
    ]
    executed: dict[str, object] = {}

    class FakeCursor:
        async def fetchall(self):
            return rows

    class FakeConnection:
        async def execute(self, query: str, params=None):
            executed["query"] = " ".join(query.split())
            executed["params"] = params
            return FakeCursor()

    @asynccontextmanager
    async def fake_connection_factory(settings):
        yield FakeConnection()

    monkeypatch.setattr(
        repository,
        "open_database_connection",
        fake_connection_factory,
    )
    session_repository = repository.PostgresSessionRepository(
        Settings(_env_file=None)
    )

    items, has_more = asyncio.run(
        session_repository.list_page(
            user_id="configured-user",
            cursor=None,
            limit=2,
        )
    )

    assert items == [Session(**row) for row in rows[:2]]
    assert has_more is True
    assert executed["query"] == (
        "SELECT session_id, user_id, title, created_at, updated_at "
        "FROM sessions WHERE user_id = %s "
        "ORDER BY updated_at DESC, session_id DESC LIMIT %s"
    )
    assert executed["params"] == ("configured-user", 3)


def test_session_repository_continues_after_composite_cursor(monkeypatch) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions import repository
    from agent_runtime.sessions.models import SessionCursor

    updated_at = datetime.now(UTC)
    session_id = uuid4()
    executed: dict[str, object] = {}

    class FakeCursor:
        async def fetchall(self):
            return []

    class FakeConnection:
        async def execute(self, query: str, params=None):
            executed["query"] = " ".join(query.split())
            executed["params"] = params
            return FakeCursor()

    @asynccontextmanager
    async def fake_connection_factory(settings):
        yield FakeConnection()

    monkeypatch.setattr(
        repository,
        "open_database_connection",
        fake_connection_factory,
    )
    session_repository = repository.PostgresSessionRepository(
        Settings(_env_file=None)
    )
    cursor = SessionCursor(updated_at=updated_at, session_id=session_id)

    items, has_more = asyncio.run(
        session_repository.list_page(
            user_id="configured-user",
            cursor=cursor,
            limit=2,
        )
    )

    assert items == []
    assert has_more is False
    assert executed["query"] == (
        "SELECT session_id, user_id, title, created_at, updated_at "
        "FROM sessions WHERE user_id = %s "
        "AND (updated_at, session_id) < (%s, %s) "
        "ORDER BY updated_at DESC, session_id DESC LIMIT %s"
    )
    assert executed["params"] == (
        "configured-user",
        updated_at,
        session_id,
        3,
    )


def test_session_repository_renames_owned_session_and_returns_updated_row(
    monkeypatch,
) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions import repository
    from agent_runtime.sessions.models import Session

    session_id = uuid4()
    created_at = datetime(2026, 9, 13, 8, 0, tzinfo=UTC)
    updated_at = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    row = {
        "session_id": session_id,
        "user_id": "configured-user",
        "title": "新标题",
        "created_at": created_at,
        "updated_at": updated_at,
    }
    executed: dict[str, object] = {}

    class FakeCursor:
        async def fetchone(self):
            return row

    class FakeConnection:
        committed = False

        async def execute(self, query: str, params=None):
            executed["query"] = " ".join(query.split())
            executed["params"] = params
            return FakeCursor()

        async def commit(self) -> None:
            self.committed = True

    connection = FakeConnection()

    @asynccontextmanager
    async def fake_connection_factory(settings):
        yield connection

    monkeypatch.setattr(
        repository,
        "open_database_connection",
        fake_connection_factory,
    )
    session_repository = repository.PostgresSessionRepository(
        Settings(_env_file=None)
    )

    renamed = asyncio.run(
        session_repository.rename(
            session_id=session_id,
            user_id="configured-user",
            title="新标题",
            updated_at=updated_at,
        )
    )

    assert renamed == Session(**row)
    assert connection.committed is True
    assert executed["query"] == (
        "UPDATE sessions SET title = %s, updated_at = %s "
        "WHERE session_id = %s AND user_id = %s "
        "RETURNING session_id, user_id, title, created_at, updated_at"
    )
    assert executed["params"] == (
        "新标题",
        updated_at,
        session_id,
        "configured-user",
    )


def test_session_repository_hides_missing_or_unowned_session_on_rename(
    monkeypatch,
) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions import repository

    class FakeCursor:
        async def fetchone(self):
            return None

    class FakeConnection:
        async def execute(self, query: str, params=None):
            return FakeCursor()

        async def commit(self) -> None:
            pass

    @asynccontextmanager
    async def fake_connection_factory(settings):
        yield FakeConnection()

    monkeypatch.setattr(
        repository,
        "open_database_connection",
        fake_connection_factory,
    )
    session_repository = repository.PostgresSessionRepository(
        Settings(_env_file=None)
    )

    with pytest.raises(repository.SessionNotFoundError) as captured:
        asyncio.run(
            session_repository.rename(
                session_id=uuid4(),
                user_id="configured-user",
                title="新标题",
                updated_at=datetime.now(UTC),
            )
        )

    assert captured.value.code == "SESSION_NOT_FOUND"
    assert captured.value.message == "Session 不存在"
    assert captured.value.status_code == 404


def test_session_repository_touches_owned_session_and_returns_updated_row(
    monkeypatch,
) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.sessions import repository
    from agent_runtime.sessions.models import Session

    session_id = uuid4()
    created_at = datetime(2026, 9, 13, 8, 0, tzinfo=UTC)
    updated_at = datetime(2026, 9, 13, 13, 0, tzinfo=UTC)
    row = {
        "session_id": session_id,
        "user_id": "configured-user",
        "title": "现有标题",
        "created_at": created_at,
        "updated_at": updated_at,
    }
    executed: dict[str, object] = {}

    class FakeCursor:
        async def fetchone(self):
            return row

    class FakeConnection:
        committed = False

        async def execute(self, query: str, params=None):
            executed["query"] = " ".join(query.split())
            executed["params"] = params
            return FakeCursor()

        async def commit(self) -> None:
            self.committed = True

    connection = FakeConnection()

    @asynccontextmanager
    async def fake_connection_factory(settings):
        yield connection

    monkeypatch.setattr(
        repository,
        "open_database_connection",
        fake_connection_factory,
    )
    session_repository = repository.PostgresSessionRepository(
        Settings(_env_file=None)
    )

    touched = asyncio.run(
        session_repository.touch(
            session_id=session_id,
            user_id="configured-user",
            updated_at=updated_at,
        )
    )

    assert touched == Session(**row)
    assert connection.committed is True
    assert executed["query"] == (
        "UPDATE sessions SET updated_at = %s "
        "WHERE session_id = %s AND user_id = %s "
        "RETURNING session_id, user_id, title, created_at, updated_at"
    )
    assert executed["params"] == (
        updated_at,
        session_id,
        "configured-user",
    )
