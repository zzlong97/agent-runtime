"""Session 级 Capability 数据与动态 Child thread 清理组件。"""

import logging
from time import perf_counter
from uuid import UUID

from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event
from agent_runtime.persistence.database import open_database_connection

logger = logging.getLogger(__name__)

_SELECT_TASK_IDS_BY_SESSION = """
SELECT task_id
FROM capability_tasks
WHERE session_id = %s
ORDER BY task_id ASC
"""

_DELETE_CONTEXTS_BY_SESSION = """
DELETE FROM capability_task_contexts
WHERE session_id = %s
"""

_DELETE_OPERATIONS_BY_SESSION = """
DELETE FROM capability_operations AS operation
USING capability_tasks AS task
WHERE operation.task_id = task.task_id
  AND task.session_id = %s
"""

_DELETE_TASKS_BY_SESSION = """
DELETE FROM capability_tasks
WHERE session_id = %s
"""

_SELECT_CHILD_THREAD_IDS_BY_SESSION = """
SELECT thread_id
FROM (
    SELECT thread_id FROM checkpoints
    UNION
    SELECT thread_id FROM checkpoint_blobs
    UNION
    SELECT thread_id FROM checkpoint_writes
) AS persisted_threads
WHERE starts_with(thread_id, %s)
   OR starts_with(thread_id, %s)
ORDER BY thread_id ASC
"""


class SessionCapabilityDeletionError(ApplicationError):
    """Session 的 Capability 数据或 thread 标识收集失败。"""


class SessionCapabilityDataDeleter:
    """按外键顺序清理单个 Session 的 Task 相关数据。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def list_task_ids_by_session(
        self,
        *,
        session_id: UUID,
    ) -> tuple[UUID, ...]:
        """在删除前按稳定顺序收集 Session 的全部 Task UUID。"""

        started_at = perf_counter()
        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _SELECT_TASK_IDS_BY_SESSION,
                    (session_id,),
                )
                rows = await cursor.fetchall()
        except Exception as error:
            raise self._error(
                message="Session Capability Task 标识收集失败",
                session_id=session_id,
                error=error,
                started_at=started_at,
            ) from error
        task_ids = tuple(UUID(str(row["task_id"])) for row in rows)
        log_business_event(
            logger,
            "Session Capability Task标识收集完成",
            session_id=session_id,
            task_count=len(task_ids),
            duration_ms=self._elapsed_ms(started_at),
        )
        return task_ids

    async def delete_by_session(self, *, session_id: UUID) -> None:
        """在单事务中依次删除 Context、Operation 与 Task。"""

        started_at = perf_counter()
        log_business_event(
            logger,
            "Session Capability数据删除开始",
            session_id=session_id,
        )
        try:
            async with open_database_connection(self._settings) as connection:
                await connection.execute(
                    _DELETE_CONTEXTS_BY_SESSION,
                    (session_id,),
                )
                await connection.execute(
                    _DELETE_OPERATIONS_BY_SESSION,
                    (session_id,),
                )
                await connection.execute(
                    _DELETE_TASKS_BY_SESSION,
                    (session_id,),
                )
                await connection.commit()
        except Exception as error:
            raise self._error(
                message="Session Capability 数据删除失败",
                session_id=session_id,
                error=error,
                started_at=started_at,
            ) from error
        log_business_event(
            logger,
            "Session Capability数据删除完成",
            session_id=session_id,
            status="deleted",
            duration_ms=self._elapsed_ms(started_at),
        )

    @staticmethod
    def _elapsed_ms(started_at: float) -> float:
        return round((perf_counter() - started_at) * 1000, 3)

    @classmethod
    def _error(
        cls,
        *,
        message: str,
        session_id: UUID,
        error: Exception,
        started_at: float,
    ) -> SessionCapabilityDeletionError:
        log_business_event(
            logger,
            message,
            level=logging.ERROR,
            session_id=session_id,
            error_code="SESSION_CAPABILITY_DELETE_FAILED",
            error_type=type(error).__name__,
            duration_ms=cls._elapsed_ms(started_at),
        )
        return SessionCapabilityDeletionError(
            code="SESSION_CAPABILITY_DELETE_FAILED",
            message=message,
            retryable=True,
        )


class DynamicChildThreadCollector:
    """从 Checkpointer 表收集 Session 的动态与遗留 Child thread。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def list_thread_ids_by_session(
        self,
        *,
        session_id: UUID,
    ) -> tuple[str, ...]:
        """收集三种动态 scope 与遗留 `{session_id}:*` thread。"""

        started_at = perf_counter()
        dynamic_prefix = f"capability:v1:{session_id}:"
        legacy_prefix = f"{session_id}:"
        try:
            async with open_database_connection(self._settings) as connection:
                cursor = await connection.execute(
                    _SELECT_CHILD_THREAD_IDS_BY_SESSION,
                    (dynamic_prefix, legacy_prefix),
                )
                rows = await cursor.fetchall()
        except Exception as error:
            log_business_event(
                logger,
                "Session Child thread标识收集失败",
                level=logging.ERROR,
                session_id=session_id,
                error_code="SESSION_CHILD_THREAD_COLLECT_FAILED",
                error_type=type(error).__name__,
                duration_ms=round(
                    (perf_counter() - started_at) * 1000,
                    3,
                ),
            )
            raise SessionCapabilityDeletionError(
                code="SESSION_CHILD_THREAD_COLLECT_FAILED",
                message="Session Child thread 标识收集失败",
                retryable=True,
            ) from error
        thread_ids = tuple(str(row["thread_id"]) for row in rows)
        log_business_event(
            logger,
            "Session Child thread标识收集完成",
            session_id=session_id,
            child_thread_count=len(thread_ids),
            duration_ms=round((perf_counter() - started_at) * 1000, 3),
        )
        return thread_ids
