"""Session 全部持久化关联数据的可重试硬删除服务。"""

import logging
from typing import Protocol
from uuid import UUID

from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event

logger = logging.getLogger(__name__)


class SessionDeletionError(ApplicationError):
    """Session 关联数据删除失败时返回的稳定应用错误。"""


class ThreadCheckpointer(Protocol):
    """删除单个 LangGraph thread 所需的最小接口。"""

    async def adelete_thread(self, thread_id: str) -> None:
        """幂等删除指定 thread 的 checkpoints、blobs 和 writes。"""


class SessionFeedbackDeleter(Protocol):
    """按 Session 清理固定用户反馈所需的最小接口。"""

    async def delete_by_session(
        self,
        *,
        user_id: str,
        session_id: UUID,
    ) -> None:
        """幂等删除指定 Session 的全部反馈。"""


class SessionRowDeleter(Protocol):
    """最后删除 Session 重试锚点所需的最小接口。"""

    async def delete_owned(
        self,
        *,
        session_id: UUID,
        user_id: str,
    ) -> None:
        """幂等删除固定用户拥有的 Session 行。"""


class SessionRuntimeDeleter(Protocol):
    """按 Session 清理持久 Run 与 RuntimeEvent 的最小接口。"""

    async def list_run_ids_by_session(self, *, session_id: UUID) -> list[UUID]:
        """返回 Session 下用于清理 Redis Stream 的全部 Run UUID。"""

    async def delete_by_session(self, *, session_id: UUID) -> None:
        """先删除 RuntimeEvent，再幂等删除指定 Session 的全部 Run。"""


class SessionCapabilityDataStore(Protocol):
    """Session 删除所需的 Capability Task 数据清理接口。"""

    async def list_task_ids_by_session(
        self,
        *,
        session_id: UUID,
    ) -> tuple[UUID, ...]:
        """在删除开始前按稳定顺序收集 Session 的全部 Task UUID。"""

    async def delete_by_session(self, *, session_id: UUID) -> None:
        """依次幂等删除 Task Context、Operation 与 Task。"""


class SessionChildThreadCollector(Protocol):
    """收集 Session 全部动态与遗留 Child thread 的最小接口。"""

    async def list_thread_ids_by_session(
        self,
        *,
        session_id: UUID,
    ) -> tuple[str, ...]:
        """返回该 Session 全部实际存在的 Child thread 标识。"""


class RedisStreamCleaner(Protocol):
    """Session 删除所需的 Redis Stream 尽力清理接口。"""

    async def delete_streams(self, run_ids: list[UUID]) -> bool:
        """删除 Run Stream；失败返回 False 且不得抛出阻塞 PostgreSQL。"""


class SessionDeletionService:
    """按已确认顺序删除 Session 的全部持久化关联数据。"""

    def __init__(
        self,
        *,
        parent_checkpointer: ThreadCheckpointer,
        child_checkpointer: ThreadCheckpointer,
        feedback_store: SessionFeedbackDeleter,
        session_repository: SessionRowDeleter,
        runtime_store: SessionRuntimeDeleter | None = None,
        capability_data_deleter: SessionCapabilityDataStore,
        child_thread_collector: SessionChildThreadCollector,
        redis_stream_cleaner: RedisStreamCleaner | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._parent_checkpointer = parent_checkpointer
        self._child_checkpointer = child_checkpointer
        self._feedback_store = feedback_store
        self._session_repository = session_repository
        self._runtime_store = runtime_store
        self._capability_data_deleter = capability_data_deleter
        self._child_thread_collector = child_thread_collector
        self._redis_stream_cleaner = redis_stream_cleaner

    async def delete_persisted_data(self, *, session_id: UUID) -> None:
        """先清 Runtime 数据，再删状态与反馈，最后删除 Session 重试锚点。"""

        log_business_event(
            logger,
            "Session持久化关联数据删除开始",
            session_id=session_id,
        )
        try:
            run_ids: list[UUID] = []
            if self._runtime_store is not None:
                run_ids = await self._runtime_store.list_run_ids_by_session(
                    session_id=session_id
                )
            task_ids = (
                await self._capability_data_deleter.list_task_ids_by_session(
                    session_id=session_id
                )
            )
            child_thread_ids = (
                await self._child_thread_collector.list_thread_ids_by_session(
                    session_id=session_id
                )
            )
            log_business_event(
                logger,
                "Session删除关联标识收集完成",
                session_id=session_id,
                run_count=len(run_ids),
                task_count=len(task_ids),
                child_thread_count=len(child_thread_ids),
            )
            if self._runtime_store is not None:
                if self._redis_stream_cleaner is not None:
                    try:
                        await self._redis_stream_cleaner.delete_streams(run_ids)
                    except Exception as error:
                        log_business_event(
                            logger,
                            "Session删除Redis清理降级",
                            level=logging.WARNING,
                            session_id=session_id,
                            run_count=len(run_ids),
                            error_code="REDIS_EVENT_DELETE_FAILED",
                            error_type=type(error).__name__,
                        )
            await self._capability_data_deleter.delete_by_session(
                session_id=session_id
            )
            if self._runtime_store is not None:
                await self._runtime_store.delete_by_session(
                    session_id=session_id
                )
            for thread_id in child_thread_ids:
                await self._child_checkpointer.adelete_thread(thread_id)
            await self._parent_checkpointer.adelete_thread(str(session_id))
            await self._feedback_store.delete_by_session(
                user_id=self._settings.local_user_id,
                session_id=session_id,
            )
            await self._session_repository.delete_owned(
                session_id=session_id,
                user_id=self._settings.local_user_id,
            )
        except Exception as error:
            log_business_event(
                logger,
                "Session持久化关联数据删除失败",
                level=logging.ERROR,
                session_id=session_id,
                error_code="SESSION_DELETE_FAILED",
                error_type=type(error).__name__,
            )
            raise SessionDeletionError(
                code="SESSION_DELETE_FAILED",
                message="Session 关联数据删除失败，请重试",
                status_code=500,
                retryable=True,
            ) from error
        log_business_event(
            logger,
            "Session持久化关联数据删除完成",
            session_id=session_id,
            status="deleted",
        )
