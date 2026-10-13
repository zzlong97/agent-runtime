"""Stage 3 集成测试显式准备 Capability 权限。"""

from datetime import UTC, datetime
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent_runtime.capabilities.persistence import (
    UserCapabilityPermissionRepository,
)
from agent_runtime.capabilities.persistence_models import (
    UserCapabilityPermission,
)
from agent_runtime.core.config import Settings
from agent_runtime.persistence.database import open_database_connection
from agent_runtime.capabilities.state_scope import ChildThreadIdFactory


async def allow_builtin_capabilities(settings: Settings) -> None:
    """为当前测试用户显式授权两个仓库内测试 Capability。"""

    repository = UserCapabilityPermissionRepository(settings)
    await repository.setup()
    now = datetime.now(UTC)
    for capability_id in ("general_chat", "en_to_zh"):
        await repository.set_allowed(
            UserCapabilityPermission(
                user_id=settings.local_user_id,
                capability_id=capability_id,
                allowed=True,
                created_at=now,
                updated_at=now,
            )
        )


async def cleanup_capability_runtime_for_session(
    settings: Settings,
    session_id: UUID,
) -> None:
    """按测试隔离需要清理 S3 投影及已知动态 Child Checkpoint。"""

    async with open_database_connection(settings) as connection:
        cursor = await connection.execute(
            """
            SELECT e.run_id, e.payload
            FROM runtime_events AS e
            INNER JOIN runs AS r ON r.run_id = e.run_id
            WHERE r.session_id = %s
              AND e.event_type IN (
                  'internal.capability.invocation.completed',
                  'internal.capability.invocation.failed',
                  'internal.capability.invocation.cancelled'
              )
            """,
            (session_id,),
        )
        terminal_invocations = await cursor.fetchall()

    factory = ChildThreadIdFactory()
    thread_ids: set[str] = set()
    for row in terminal_invocations:
        payload = row["payload"]
        state_schema_version = payload.get("state_schema_version")
        task_id = payload.get("task_id")
        if state_schema_version is None or task_id is None:
            continue
        thread_ids.add(
            factory.build(
                state_scope=payload["state_scope"],
                session_id=session_id,
                capability_id=payload["capability_id"],
                state_schema_version=state_schema_version,
                run_id=UUID(str(row["run_id"])),
                invocation_id=UUID(str(payload["invocation_id"])),
                task_id=UUID(str(task_id)),
            )
        )
    async with AsyncPostgresSaver.from_conn_string(
        settings.database_connection_string
    ) as checkpointer:
        for thread_id in sorted(thread_ids):
            await checkpointer.adelete_thread(thread_id)

    async with open_database_connection(settings) as connection:
        await connection.execute(
            "DELETE FROM capability_task_contexts WHERE session_id = %s",
            (session_id,),
        )
        await connection.execute(
            "DELETE FROM capability_operations WHERE task_id IN "
            "(SELECT task_id FROM capability_tasks WHERE session_id = %s)",
            (session_id,),
        )
        await connection.execute(
            "DELETE FROM capability_tasks WHERE session_id = %s",
            (session_id,),
        )
        await connection.commit()
