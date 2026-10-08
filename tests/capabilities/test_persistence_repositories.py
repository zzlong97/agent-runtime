import asyncio
from contextlib import asynccontextmanager


def _normalize(query: str) -> str:
    return " ".join(query.split())


def test_persistence_contracts_are_exported_from_capabilities_package() -> None:
    from agent_runtime.capabilities import (
        CapabilityOperation,
        CapabilityOperationRepository,
        CapabilityTask,
        CapabilityTaskContextRepository,
        CapabilityTaskRepository,
        UserCapabilityPermission,
        UserCapabilityPermissionRepository,
    )

    assert CapabilityTask.__name__ == "CapabilityTask"
    assert CapabilityOperation.__name__ == "CapabilityOperation"
    assert UserCapabilityPermission.__name__ == "UserCapabilityPermission"
    assert CapabilityTaskRepository.__name__ == "CapabilityTaskRepository"
    assert (
        CapabilityTaskContextRepository.__name__
        == "CapabilityTaskContextRepository"
    )
    assert (
        CapabilityOperationRepository.__name__
        == "CapabilityOperationRepository"
    )
    assert (
        UserCapabilityPermissionRepository.__name__
        == "UserCapabilityPermissionRepository"
    )


def test_setup_creates_four_strict_tables_and_required_indexes(monkeypatch) -> None:
    from agent_runtime.capabilities import persistence
    from agent_runtime.capabilities.persistence import (
        CapabilityOperationRepository,
        CapabilityTaskContextRepository,
        CapabilityTaskRepository,
        UserCapabilityPermissionRepository,
    )
    from agent_runtime.core.config import Settings

    statements: list[str] = []

    class FakeConnection:
        committed = 0

        async def execute(self, query: str, params=None):
            statements.append(_normalize(query))

        async def commit(self) -> None:
            self.committed += 1

    connection = FakeConnection()

    @asynccontextmanager
    async def fake_connection_factory(settings):
        yield connection

    monkeypatch.setattr(
        persistence,
        "open_database_connection",
        fake_connection_factory,
    )
    settings = Settings(_env_file=None)

    async def exercise() -> None:
        await CapabilityTaskRepository(settings).setup()
        await CapabilityTaskContextRepository(settings).setup()
        await CapabilityOperationRepository(settings).setup()
        await UserCapabilityPermissionRepository(settings).setup()

    asyncio.run(exercise())

    sql = "\n".join(statements)
    assert connection.committed == 4
    assert "CREATE TABLE IF NOT EXISTS capability_tasks" in sql
    assert "status IN ('active', 'completed', 'failed', 'cancelled')" in sql
    assert "ON DELETE RESTRICT" in sql
    assert "CREATE TABLE IF NOT EXISTS capability_task_contexts" in sql
    assert "PRIMARY KEY (session_id, capability_id)" in sql
    assert "UNIQUE (current_task_id)" in sql
    assert (
        "FOREIGN KEY (current_task_id, session_id, capability_id) "
        "REFERENCES capability_tasks (task_id, session_id, capability_id)"
    ) in sql
    assert "CREATE TABLE IF NOT EXISTS capability_operations" in sql
    assert "UNIQUE (run_id, invocation_id, operation_key)" in sql
    assert "UNIQUE (idempotency_key)" in sql
    assert "CREATE TABLE IF NOT EXISTS user_capability_permissions" in sql
    assert "PRIMARY KEY (user_id, capability_id)" in sql
    assert "CREATE INDEX IF NOT EXISTS capability_tasks_lookup_idx" in sql
    assert "CREATE INDEX IF NOT EXISTS capability_operations_task_idx" in sql
    assert "CREATE INDEX IF NOT EXISTS user_capability_permissions_lookup_idx" in sql


def test_permission_repository_preserves_missing_false_and_true(monkeypatch) -> None:
    from agent_runtime.capabilities import persistence
    from agent_runtime.capabilities.persistence import (
        UserCapabilityPermissionRepository,
    )
    from agent_runtime.core.config import Settings

    rows = [None, {"allowed": False}, {"allowed": True}]

    class FakeCursor:
        async def fetchone(self):
            return rows.pop(0)

    class FakeConnection:
        async def execute(self, query: str, params=None):
            return FakeCursor()

    @asynccontextmanager
    async def fake_connection_factory(settings):
        yield FakeConnection()

    monkeypatch.setattr(
        persistence,
        "open_database_connection",
        fake_connection_factory,
    )
    repository = UserCapabilityPermissionRepository(Settings(_env_file=None))

    async def exercise() -> tuple[bool | None, bool | None, bool | None]:
        return (
            await repository.get_allowed(
                user_id="runtime-user",
                capability_id="general_chat",
            ),
            await repository.get_allowed(
                user_id="runtime-user",
                capability_id="general_chat",
            ),
            await repository.get_allowed(
                user_id="runtime-user",
                capability_id="general_chat",
            ),
        )

    assert asyncio.run(exercise()) == (None, False, True)


def test_current_task_upsert_locks_active_task_row() -> None:
    from agent_runtime.capabilities import persistence

    sql = _normalize(persistence._UPSERT_CONTEXT)

    assert "WITH target_task AS" in sql
    assert "status = 'active' FOR UPDATE" in sql
