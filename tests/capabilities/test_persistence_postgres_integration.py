import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation


def run_on_psycopg_compatible_loop(coroutine):
    """使用 psycopg 在 Windows 下兼容的事件循环执行协程。"""

    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coroutine)


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_TESTS") != "1",
        reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
    ),
]


def test_capability_persistence_setup_is_reentrant_on_existing_database() -> None:
    from agent_runtime.capabilities.persistence import (
        CapabilityOperationRepository,
        CapabilityTaskContextRepository,
        CapabilityTaskRepository,
        UserCapabilityPermissionRepository,
    )
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s3-04-setup-{uuid4()}",
        _env_file=None,
    )
    session_id = uuid4()
    now = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    session_repository = PostgresSessionRepository(settings)
    run_repository = PostgresRunRepository(settings)
    repositories = (
        CapabilityTaskRepository(settings),
        CapabilityTaskContextRepository(settings),
        CapabilityOperationRepository(settings),
        UserCapabilityPermissionRepository(settings),
    )

    async def exercise() -> None:
        await session_repository.setup()
        await run_repository.setup()
        await session_repository.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="升级前会话",
                created_at=now,
                updated_at=now,
            )
        )
        try:
            for _ in range(2):
                for repository in repositories:
                    await repository.setup()

            async with open_database_connection(settings) as connection:
                cursor = await connection.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = ANY(%s)
                    ORDER BY table_name
                    """,
                    (
                        [
                            "capability_tasks",
                            "capability_task_contexts",
                            "capability_operations",
                            "user_capability_permissions",
                        ],
                    ),
                )
                rows = await cursor.fetchall()
                preserved = await connection.execute(
                    "SELECT title FROM sessions WHERE session_id = %s",
                    (session_id,),
                )
                session_row = await preserved.fetchone()
            assert [row["table_name"] for row in rows] == [
                "capability_operations",
                "capability_task_contexts",
                "capability_tasks",
                "user_capability_permissions",
            ]
            assert session_row == {"title": "升级前会话"}
        finally:
            async with open_database_connection(settings) as connection:
                await connection.execute(
                    "DELETE FROM sessions WHERE session_id = %s",
                    (session_id,),
                )
                await connection.commit()

    run_on_psycopg_compatible_loop(exercise())


def test_task_context_constraints_concurrency_and_atomic_rollback() -> None:
    from agent_runtime.capabilities.persistence import (
        CapabilityPersistenceConflictError,
        CapabilityTaskContextRepository,
        CapabilityTaskRepository,
    )
    from agent_runtime.capabilities.persistence_models import (
        CapabilityTask,
        CapabilityTaskContext,
    )
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s3-04-task-{uuid4()}",
        _env_file=None,
    )
    session_id = uuid4()
    missing_session_id = uuid4()
    now = datetime(2026, 10, 8, 11, 0, tzinfo=UTC)
    session_repository = PostgresSessionRepository(settings)
    task_repository = CapabilityTaskRepository(settings)
    context_repository = CapabilityTaskContextRepository(settings)

    def build_task(*, session= session_id) -> CapabilityTask:
        return CapabilityTask(
            task_id=uuid4(),
            session_id=session,
            capability_id="general_chat",
            state_schema_version="v1",
            status="active",
            last_run_id=None,
            last_invocation_id=None,
            created_at=now,
            updated_at=now,
            ended_at=None,
        )

    async def exercise() -> None:
        await session_repository.setup()
        await PostgresRunRepository(settings).setup()
        await task_repository.setup()
        await context_repository.setup()
        await session_repository.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="Task 约束",
                created_at=now,
                updated_at=now,
            )
        )
        first = build_task()
        second = build_task()
        third = build_task()
        rolled_back = build_task(session=missing_session_id)
        try:
            await task_repository.create_and_set_current(first)
            assert await context_repository.get(
                session_id=session_id,
                capability_id="general_chat",
            ) == CapabilityTaskContext(
                session_id=session_id,
                capability_id="general_chat",
                current_task_id=first.task_id,
                updated_at=now,
            )

            await asyncio.gather(
                task_repository.create_and_set_current(second),
                task_repository.create_and_set_current(third),
            )
            current = await context_repository.get(
                session_id=session_id,
                capability_id="general_chat",
            )
            assert current is not None
            assert current.current_task_id in {second.task_id, third.task_id}
            assert await task_repository.get(second.task_id) == second
            assert await task_repository.get(third.task_id) == third

            with pytest.raises(
                CapabilityPersistenceConflictError,
                match="current Task 必须是同 Session、同 Capability 的 active Task",
            ):
                await context_repository.set_current(
                    CapabilityTaskContext(
                        session_id=session_id,
                        capability_id="en_to_zh",
                        current_task_id=first.task_id,
                        updated_at=now,
                    )
                )

            with pytest.raises(
                CapabilityPersistenceConflictError,
                match="所属 Session 不存在",
            ):
                await task_repository.create_and_set_current(rolled_back)
            assert await task_repository.get(rolled_back.task_id) is None

            async with open_database_connection(settings) as connection:
                with pytest.raises(ForeignKeyViolation):
                    await connection.execute(
                        """
                        INSERT INTO capability_task_contexts (
                            session_id, capability_id, current_task_id, updated_at
                        ) VALUES (%s, %s, %s, %s)
                        """,
                        (session_id, "en_to_zh", first.task_id, now),
                    )
                await connection.rollback()

            async with open_database_connection(settings) as connection:
                with pytest.raises(CheckViolation):
                    await connection.execute(
                        """
                        INSERT INTO capability_tasks (
                            task_id, session_id, capability_id,
                            state_schema_version, status, created_at,
                            updated_at, ended_at
                        ) VALUES (%s, %s, %s, %s, 'queued', %s, %s, NULL)
                        """,
                        (
                            uuid4(),
                            session_id,
                            "general_chat",
                            "v1",
                            now,
                            now,
                        ),
                    )
                await connection.rollback()

            async with open_database_connection(settings) as connection:
                with pytest.raises(CheckViolation):
                    await connection.execute(
                        """
                        INSERT INTO capability_tasks (
                            task_id, session_id, capability_id,
                            state_schema_version, status, created_at,
                            updated_at, ended_at
                        ) VALUES (%s, %s, %s, %s, 'failed', %s, %s, NULL)
                        """,
                        (
                            uuid4(),
                            session_id,
                            "general_chat",
                            "v1",
                            now,
                            now,
                        ),
                    )
                await connection.rollback()

            finished_at = now + timedelta(minutes=1)
            finished = await task_repository.finish_and_clear_current(
                task_id=current.current_task_id,
                status="completed",
                last_run_id=None,
                last_invocation_id=uuid4(),
                updated_at=finished_at,
                ended_at=finished_at,
            )
            assert finished.status == "completed"
            assert finished.ended_at == finished_at
            assert await context_repository.get(
                session_id=session_id,
                capability_id="general_chat",
            ) is None

            async with open_database_connection(settings) as connection:
                with pytest.raises(CheckViolation):
                    await connection.execute(
                        """
                        INSERT INTO capability_tasks (
                            task_id, session_id, capability_id,
                            state_schema_version, status, created_at,
                            updated_at, ended_at
                        ) VALUES (%s, %s, %s, %s, 'active', %s, %s, %s)
                        """,
                        (
                            uuid4(),
                            session_id,
                            "general_chat",
                            "v1",
                            now,
                            now,
                            now,
                        ),
                    )
                await connection.rollback()
        finally:
            async with open_database_connection(settings) as connection:
                await connection.execute(
                    "DELETE FROM capability_task_contexts WHERE session_id = %s",
                    (session_id,),
                )
                await connection.execute(
                    "DELETE FROM capability_tasks WHERE session_id = %s",
                    (session_id,),
                )
                await connection.execute(
                    "DELETE FROM sessions WHERE session_id = %s",
                    (session_id,),
                )
                await connection.commit()

    run_on_psycopg_compatible_loop(exercise())


def test_operation_uniqueness_relationships_and_permission_tristate() -> None:
    from agent_runtime.capabilities.persistence import (
        CapabilityOperationRepository,
        CapabilityPersistenceConflictError,
        CapabilityTaskContextRepository,
        CapabilityTaskRepository,
        UserCapabilityPermissionRepository,
    )
    from agent_runtime.capabilities.persistence_models import (
        CapabilityOperation,
        CapabilityTask,
        UserCapabilityPermission,
    )
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.models import RunSubmission
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s3-04-operation-{uuid4()}",
        _env_file=None,
    )
    session_id = uuid4()
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    session_repository = PostgresSessionRepository(settings)
    run_repository = PostgresRunRepository(settings)
    task_repository = CapabilityTaskRepository(settings)
    context_repository = CapabilityTaskContextRepository(settings)
    operation_repository = CapabilityOperationRepository(settings)
    permission_repository = UserCapabilityPermissionRepository(settings)
    submission = RunSubmission(
        run_id=uuid4(),
        request_id=uuid4(),
        session_id=session_id,
        thread_id=str(session_id),
        parent_run_id=None,
        run_type="normal",
        input_message_id=uuid4(),
        response_message_id=uuid4(),
        start_checkpoint_id=None,
        input_payload={"message": {"content": "仅用于测试，不进入日志"}},
        request_fingerprint=build_request_fingerprint(
            run_type="normal",
            session_id=session_id,
            request_payload={"message": {"content": "仅用于测试"}},
        ),
        created_at=now,
    )
    task = CapabilityTask(
        task_id=uuid4(),
        session_id=session_id,
        capability_id="general_chat",
        state_schema_version="v1",
        status="active",
        last_run_id=None,
        last_invocation_id=None,
        created_at=now,
        updated_at=now,
        ended_at=None,
    )
    operation = CapabilityOperation(
        operation_id=uuid4(),
        run_id=submission.run_id,
        invocation_id=uuid4(),
        task_id=task.task_id,
        capability_id="general_chat",
        operation_key="send-email",
        idempotency_key="a" * 64,
        status="pending",
        created_at=now,
        updated_at=now,
    )

    async def exercise() -> None:
        await session_repository.setup()
        await run_repository.setup()
        await task_repository.setup()
        await context_repository.setup()
        await operation_repository.setup()
        await permission_repository.setup()
        await session_repository.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="Operation 约束",
                created_at=now,
                updated_at=now,
            )
        )
        try:
            await run_repository.create_or_get(submission)
            await task_repository.create_and_set_current(task)
            created, repeated = await asyncio.gather(
                operation_repository.create_or_get(operation),
                operation_repository.create_or_get(
                    replace(operation, operation_id=uuid4())
                ),
            )
            assert {created.created, repeated.created} == {True, False}
            assert repeated.operation == created.operation

            other_task = replace(task, task_id=uuid4())
            await task_repository.add(other_task)
            with pytest.raises(CapabilityPersistenceConflictError):
                await operation_repository.create_or_get(
                    replace(
                        operation,
                        operation_id=uuid4(),
                        task_id=other_task.task_id,
                    )
                )

            with pytest.raises(CapabilityPersistenceConflictError):
                await operation_repository.create_or_get(
                    replace(
                        operation,
                        operation_id=uuid4(),
                        idempotency_key="b" * 64,
                    )
                )
            with pytest.raises(CapabilityPersistenceConflictError):
                await operation_repository.create_or_get(
                    replace(
                        operation,
                        operation_id=uuid4(),
                        operation_key="send-sms",
                    )
                )
            with pytest.raises(CapabilityPersistenceConflictError):
                await operation_repository.create_or_get(
                    replace(
                        operation,
                        operation_id=uuid4(),
                        operation_key="other-capability",
                        idempotency_key="c" * 64,
                        capability_id="en_to_zh",
                    )
                )

            succeeded = await operation_repository.set_status(
                operation_id=created.operation.operation_id,
                status="succeeded",
                updated_at=now + timedelta(minutes=1),
            )
            assert succeeded.status == "succeeded"
            assert not hasattr(succeeded, "response_payload")
            assert not hasattr(succeeded, "error_detail")

            assert await permission_repository.get_allowed(
                user_id=settings.local_user_id,
                capability_id="general_chat",
            ) is None
            denied = UserCapabilityPermission(
                user_id=settings.local_user_id,
                capability_id="general_chat",
                allowed=False,
                created_at=now,
                updated_at=now,
            )
            await asyncio.gather(
                permission_repository.set_allowed(denied),
                permission_repository.set_allowed(
                    replace(
                        denied,
                        allowed=True,
                        updated_at=now + timedelta(milliseconds=1),
                    )
                ),
            )
            assert await permission_repository.get_allowed(
                user_id=settings.local_user_id,
                capability_id="general_chat",
            ) in {False, True}
            await permission_repository.set_allowed(denied)
            assert await permission_repository.get_allowed(
                user_id=settings.local_user_id,
                capability_id="general_chat",
            ) is False
            await permission_repository.set_allowed(
                replace(denied, allowed=True, updated_at=now + timedelta(seconds=1))
            )
            assert await permission_repository.get_allowed(
                user_id=settings.local_user_id,
                capability_id="general_chat",
            ) is True
            await permission_repository.revoke(
                user_id=settings.local_user_id,
                capability_id="general_chat",
            )
            assert await permission_repository.get_allowed(
                user_id=settings.local_user_id,
                capability_id="general_chat",
            ) is None
        finally:
            async with open_database_connection(settings) as connection:
                await connection.execute(
                    "DELETE FROM user_capability_permissions WHERE user_id = %s",
                    (settings.local_user_id,),
                )
                await connection.execute(
                    "DELETE FROM capability_operations WHERE task_id = %s",
                    (task.task_id,),
                )
                await connection.execute(
                    "DELETE FROM capability_task_contexts WHERE session_id = %s",
                    (session_id,),
                )
                await connection.execute(
                    "DELETE FROM capability_tasks WHERE session_id = %s",
                    (session_id,),
                )
                await connection.execute(
                    "DELETE FROM runs WHERE session_id = %s",
                    (session_id,),
                )
                await connection.execute(
                    "DELETE FROM sessions WHERE session_id = %s",
                    (session_id,),
                )
                await connection.commit()

    run_on_psycopg_compatible_loop(exercise())
