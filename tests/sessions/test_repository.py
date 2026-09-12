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
    assert len(statements) == 1
    assert "CREATE TABLE IF NOT EXISTS sessions" in statements[0]
    for column in (
        "session_id UUID PRIMARY KEY",
        "user_id TEXT NOT NULL",
        "title TEXT NOT NULL",
        "created_at TIMESTAMPTZ NOT NULL",
        "updated_at TIMESTAMPTZ NOT NULL",
    ):
        assert column in statements[0]


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
