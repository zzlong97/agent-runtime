"""S3-13 Session 动态 Capability 数据与 Child Checkpoint 删除验收。"""

import asyncio
import os
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from redis.asyncio import Redis


async def _store_checkpoint(checkpointer, *, thread_id: str) -> None:
    """写入一份只用于验证 thread 删除的最小 Checkpoint。"""

    version = checkpointer.get_next_version(None, None)
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"]["messages"] = [
        HumanMessage(content="删除测试状态", id=str(uuid4()))
    ]
    checkpoint["channel_versions"]["messages"] = version
    checkpoint["updated_channels"] = ["messages"]
    await checkpointer.aput(
        {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
            }
        },
        checkpoint,
        {"source": "input", "step": -1, "parents": {}},
        {"messages": version},
    )


@pytest.mark.postgres
@pytest.mark.redis
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1"
    or os.getenv("RUN_REDIS_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 和 RUN_REDIS_TESTS=1 后运行真实依赖测试",
)
def test_session_deletion_removes_s3_data_and_all_child_thread_scopes() -> None:
    """多 Run、Task、Capability 与三种 scope 均删除，其他 Session 保持不变。"""

    from agent_runtime.capabilities.persistence import (
        CapabilityOperationRepository,
        CapabilityTaskContextRepository,
        CapabilityTaskRepository,
        UserCapabilityPermissionRepository,
    )
    from agent_runtime.capabilities.persistence_models import (
        CapabilityOperation,
        CapabilityTask,
        UserCapabilityPermission,
    )
    from agent_runtime.capabilities.session_deletion import (
        DynamicChildThreadCollector,
        SessionCapabilityDataDeleter,
    )
    from agent_runtime.capabilities.state_scope import ChildThreadIdFactory
    from agent_runtime.core.config import Settings
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
    from agent_runtime.feedback import PostgresFeedbackStore
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.models import RunSubmission
    from agent_runtime.runtime.redis_stream import RedisStreamPublisher
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.session_deletion import SessionDeletionService
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s3-13-delete-{uuid4()}",
        _env_file=None,
    )
    target_session_id = uuid4()
    other_session_id = uuid4()
    now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    target_run_ids = (uuid4(), uuid4())
    other_run_id = uuid4()
    target_task_ids = (uuid4(), uuid4(), uuid4())
    other_task_id = uuid4()
    target_invocation_ids = (uuid4(), uuid4(), uuid4())
    other_invocation_id = uuid4()
    capability_ids = ("alpha_agent", "beta_agent", "gamma_agent")
    thread_factory = ChildThreadIdFactory()

    def submission(*, session_id: UUID, run_id: UUID) -> RunSubmission:
        payload = {"message": {"content": "删除矩阵测试"}}
        return RunSubmission(
            run_id=run_id,
            request_id=uuid4(),
            session_id=session_id,
            thread_id=str(session_id),
            parent_run_id=None,
            run_type="normal",
            input_message_id=uuid4(),
            response_message_id=uuid4(),
            start_checkpoint_id=None,
            input_payload=payload,
            request_fingerprint=build_request_fingerprint(
                run_type="normal",
                session_id=session_id,
                request_payload=payload,
            ),
            created_at=now,
        )

    async def finish_run(run_id: UUID) -> None:
        async with open_database_connection(settings) as connection:
            await connection.execute(
                "UPDATE runs SET status = 'completed', input_payload = NULL, "
                "started_at = %s, finished_at = %s, updated_at = %s "
                "WHERE run_id = %s",
                (now, now, now, run_id),
            )
            await connection.commit()

    async def exercise() -> None:
        redis = Redis.from_url(
            settings.redis_connection_string,
            encoding="utf-8",
            decode_responses=True,
        )
        redis_cleaner = RedisStreamPublisher.from_url(
            settings.redis_connection_string,
            ttl_seconds=settings.redis_stream_ttl_seconds,
            socket_timeout_seconds=settings.redis_socket_timeout_seconds,
        )
        sessions = PostgresSessionRepository(settings)
        runs = PostgresRunRepository(settings)
        tasks = CapabilityTaskRepository(settings)
        contexts = CapabilityTaskContextRepository(settings)
        operations = CapabilityOperationRepository(settings)
        permissions = UserCapabilityPermissionRepository(settings)
        feedback = PostgresFeedbackStore(settings)
        await sessions.setup()
        await runs.setup()
        await tasks.setup()
        await contexts.setup()
        await operations.setup()
        await permissions.setup()
        await feedback.setup()
        for session_id, title in (
            (target_session_id, "待删除 Session"),
            (other_session_id, "保留 Session"),
        ):
            await sessions.add(
                Session(
                    session_id=session_id,
                    user_id=settings.local_user_id,
                    title=title,
                    created_at=now,
                    updated_at=now,
                )
            )

        for run_id in target_run_ids:
            await runs.create_or_get(
                submission(session_id=target_session_id, run_id=run_id)
            )
            await finish_run(run_id)
        await runs.create_or_get(
            submission(session_id=other_session_id, run_id=other_run_id)
        )
        await finish_run(other_run_id)

        target_tasks = tuple(
            CapabilityTask(
                task_id=task_id,
                session_id=target_session_id,
                capability_id=capability_id,
                state_schema_version="v1",
                status="active",
                last_run_id=target_run_ids[index % len(target_run_ids)],
                last_invocation_id=target_invocation_ids[index],
                created_at=now,
                updated_at=now,
                ended_at=None,
            )
            for index, (task_id, capability_id) in enumerate(
                zip(target_task_ids, capability_ids, strict=True)
            )
        )
        for task in target_tasks:
            await tasks.create_and_set_current(task)
        other_task = CapabilityTask(
            task_id=other_task_id,
            session_id=other_session_id,
            capability_id="other_agent",
            state_schema_version="v1",
            status="active",
            last_run_id=other_run_id,
            last_invocation_id=other_invocation_id,
            created_at=now,
            updated_at=now,
            ended_at=None,
        )
        await tasks.create_and_set_current(other_task)

        for index, task in enumerate((*target_tasks, other_task)):
            await operations.create_or_get(
                CapabilityOperation(
                    operation_id=uuid4(),
                    run_id=(
                        target_run_ids[index % len(target_run_ids)]
                        if task.session_id == target_session_id
                        else other_run_id
                    ),
                    invocation_id=task.last_invocation_id,
                    task_id=task.task_id,
                    capability_id=task.capability_id,
                    operation_key=f"operation-{index}",
                    idempotency_key=sha256(
                        f"operation-{index}".encode("utf-8")
                    ).hexdigest(),
                    status="pending",
                    created_at=now,
                    updated_at=now,
                )
            )

        await permissions.set_allowed(
            UserCapabilityPermission(
                user_id=settings.local_user_id,
                capability_id="alpha_agent",
                allowed=True,
                created_at=now,
                updated_at=now,
            )
        )
        for run_id in (*target_run_ids, other_run_id):
            await redis.xadd(
                f"runtime:events:{run_id}",
                {"event": "deletion-test"},
                id="1-0",
            )

        target_threads = (
            thread_factory.build(
                state_scope="invocation",
                session_id=target_session_id,
                capability_id="alpha_agent",
                state_schema_version="v1",
                invocation_id=target_invocation_ids[0],
            ),
            thread_factory.build(
                state_scope="run",
                session_id=target_session_id,
                capability_id="beta_agent",
                state_schema_version="v1",
                run_id=target_run_ids[1],
            ),
            thread_factory.build(
                state_scope="session",
                session_id=target_session_id,
                capability_id="gamma_agent",
                state_schema_version="v1",
                task_id=target_task_ids[2],
            ),
            f"{target_session_id}:general_chat",
            f"{target_session_id}:retired_agent",
        )
        other_thread = thread_factory.build(
            state_scope="session",
            session_id=other_session_id,
            capability_id="other_agent",
            state_schema_version="v1",
            task_id=other_task_id,
        )

        async with AsyncPostgresSaver.from_conn_string(
            settings.database_connection_string
        ) as checkpointer:
            await checkpointer.setup()
            for thread_id in (
                str(target_session_id),
                *target_threads,
                str(other_session_id),
                other_thread,
            ):
                await _store_checkpoint(checkpointer, thread_id=thread_id)

            capability_data = SessionCapabilityDataDeleter(settings)
            child_threads = DynamicChildThreadCollector(settings)
            assert await capability_data.list_task_ids_by_session(
                session_id=target_session_id
            ) == tuple(sorted(target_task_ids, key=str))
            assert await child_threads.list_thread_ids_by_session(
                session_id=target_session_id
            ) == tuple(sorted(target_threads))

            deletion = SessionDeletionService(
                settings=settings,
                parent_checkpointer=checkpointer,
                child_checkpointer=checkpointer,
                feedback_store=feedback,
                session_repository=sessions,
                runtime_store=runs,
                capability_data_deleter=capability_data,
                child_thread_collector=child_threads,
                redis_stream_cleaner=redis_cleaner,
            )
            await deletion.delete_persisted_data(
                session_id=target_session_id
            )

            for deleted_thread_id in (
                str(target_session_id),
                *target_threads,
            ):
                assert await checkpointer.aget_tuple(
                    {"configurable": {"thread_id": deleted_thread_id}}
                ) is None
            assert await checkpointer.aget_tuple(
                {"configurable": {"thread_id": str(other_session_id)}}
            ) is not None
            assert await checkpointer.aget_tuple(
                {"configurable": {"thread_id": other_thread}}
            ) is not None
            target_stream_counts = [
                await redis.exists(f"runtime:events:{run_id}")
                for run_id in target_run_ids
            ]
            assert target_stream_counts == [0, 0]
            assert await redis.exists(f"runtime:events:{other_run_id}") == 1

        async with open_database_connection(settings) as connection:
            expected = {
                "sessions": ("session_id", target_session_id, 0),
                "runs": ("session_id", target_session_id, 0),
                "capability_tasks": ("session_id", target_session_id, 0),
                "capability_task_contexts": (
                    "session_id",
                    target_session_id,
                    0,
                ),
            }
            for table, (column, value, total) in expected.items():
                cursor = await connection.execute(
                    f"SELECT COUNT(*) AS total FROM {table} WHERE {column} = %s",
                    (value,),
                )
                row = await cursor.fetchone()
                assert row is not None and row["total"] == total
            operation_cursor = await connection.execute(
                "SELECT COUNT(*) AS total FROM capability_operations "
                "WHERE task_id = ANY(%s)",
                (list(target_task_ids),),
            )
            operation_row = await operation_cursor.fetchone()
            assert operation_row is not None
            assert operation_row["total"] == 0
            permission_cursor = await connection.execute(
                "SELECT COUNT(*) AS total FROM user_capability_permissions "
                "WHERE user_id = %s AND capability_id = 'alpha_agent'",
                (settings.local_user_id,),
            )
            permission_row = await permission_cursor.fetchone()
            assert permission_row is not None
            assert permission_row["total"] == 1
            other_cursor = await connection.execute(
                "SELECT COUNT(*) AS total FROM capability_tasks "
                "WHERE session_id = %s",
                (other_session_id,),
            )
            other_row = await other_cursor.fetchone()
            assert other_row is not None and other_row["total"] == 1

        async with AsyncPostgresSaver.from_conn_string(
            settings.database_connection_string
        ) as cleanup_checkpointer:
            cleanup = SessionDeletionService(
                settings=settings,
                parent_checkpointer=cleanup_checkpointer,
                child_checkpointer=cleanup_checkpointer,
                feedback_store=feedback,
                session_repository=sessions,
                runtime_store=runs,
                capability_data_deleter=capability_data,
                child_thread_collector=child_threads,
                redis_stream_cleaner=redis_cleaner,
            )
            await cleanup.delete_persisted_data(session_id=other_session_id)
        await permissions.revoke(
            user_id=settings.local_user_id,
            capability_id="alpha_agent",
        )
        await redis_cleaner.aclose()
        await redis.aclose()

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(exercise())
