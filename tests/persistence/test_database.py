import asyncio
from typing import Any


def test_database_connection_uses_configured_postgresql_dsn_and_closes(
    monkeypatch,
) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence import database

    captured: dict[str, Any] = {}

    class FakeConnection:
        closed = False

        async def close(self) -> None:
            self.closed = True

    connection = FakeConnection()

    class FakeAsyncConnection:
        @classmethod
        async def connect(cls, conninfo: str, **kwargs: Any) -> FakeConnection:
            captured["conninfo"] = conninfo
            captured["kwargs"] = kwargs
            return connection

    monkeypatch.setattr(database, "AsyncConnection", FakeAsyncConnection)
    settings = Settings(
        database_url="postgresql://runtime:secret@localhost:5432/agent_runtime",
        _env_file=None,
    )

    async def exercise_connection() -> None:
        async with database.open_database_connection(settings) as opened:
            assert opened is connection
            assert connection.closed is False

    asyncio.run(exercise_connection())

    assert captured["conninfo"] == (
        "postgresql://runtime:secret@localhost:5432/agent_runtime"
    )
    assert captured["kwargs"]["row_factory"] is database.dict_row
    assert connection.closed is True
