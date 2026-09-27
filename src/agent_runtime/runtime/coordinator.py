"""Stage 2.5 单进程 Run 唤醒与 queued 补偿协调。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from uuid import UUID

from agent_runtime.core.logging import log_business_event
from agent_runtime.runtime.models import Run
from agent_runtime.runtime.repository import PostgresRunRepository

RunExecutor = Callable[[Run], Awaitable[None]]

logger = logging.getLogger(__name__)


class RunCoordinator:
    """在单个 Runtime 进程内去重执行任务并补偿丢失唤醒。"""

    def __init__(
        self,
        *,
        repository: PostgresRunRepository,
        user_id: str,
        scan_interval_seconds: float,
    ) -> None:
        if scan_interval_seconds <= 0:
            raise ValueError("Coordinator 扫描间隔必须大于 0 秒")
        self._repository = repository
        self._user_id = user_id
        self._scan_interval_seconds = scan_interval_seconds
        self._executor: RunExecutor | None = None
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._scan_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def start(
        self,
        executor: RunExecutor,
        *,
        scan_on_startup: bool = True,
    ) -> None:
        """绑定本地执行器，完成启动扫描后启动周期补偿。"""

        if self._executor is not None:
            raise RuntimeError("Coordinator 不允许重复启动")
        self._executor = executor
        log_business_event(logger, "Run协调器启动开始")
        if scan_on_startup:
            await self.scan_once(reason="startup")
        self._scan_task = asyncio.create_task(
            self._scan_loop(),
            name="run-coordinator-scan",
        )
        log_business_event(logger, "Run协调器启动完成")

    async def wake(self, run_id: UUID) -> None:
        """在 Run 事务提交后尝试低延迟唤醒；丢失后由扫描补偿。"""

        if self._closed:
            return
        try:
            run = await self._repository.get(run_id)
        except Exception as error:
            log_business_event(
                logger,
                "Run协调器唤醒降级",
                level=logging.WARNING,
                run_id=run_id,
                error_code="RUN_COORDINATOR_WAKE_FAILED",
                error_type=type(error).__name__,
            )
            return
        await self._dispatch_if_queued(run, reason="submit")

    async def scan_once(self, *, reason: str = "compensation") -> None:
        """扫描固定用户的非终态 Run，本阶段只分派 queued Run。"""

        started_at = perf_counter()
        try:
            runs = await self._repository.list_active_for_user(
                user_id=self._user_id
            )
        except Exception as error:
            log_business_event(
                logger,
                "Run协调器扫描失败",
                level=logging.ERROR,
                scan_reason=reason,
                error_code="RUN_COORDINATOR_SCAN_FAILED",
                error_type=type(error).__name__,
                duration_ms=_elapsed_ms(started_at),
            )
            return
        queued_count = 0
        deferred_count = 0
        for run in runs:
            if run.status == "queued":
                queued_count += 1
                await self._dispatch_if_queued(run, reason=reason)
            else:
                # running/recovering 的对账恢复属于 S2.5-08。
                deferred_count += 1
        log_business_event(
            logger,
            "Run协调器扫描完成",
            scan_reason=reason,
            active_count=len(runs),
            queued_count=queued_count,
            deferred_count=deferred_count,
            duration_ms=_elapsed_ms(started_at),
        )

    async def wait_until_idle(self) -> None:
        """等待已分派的本地任务结束，仅用于关闭与确定性验证。"""

        while True:
            async with self._lock:
                tasks = tuple(self._tasks.values())
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        """停止扫描并取消本地任务，持久 Run 留给后续恢复。"""

        self._closed = True
        scan_task = self._scan_task
        self._scan_task = None
        if scan_task is not None:
            scan_task.cancel()
            await asyncio.gather(scan_task, return_exceptions=True)
        async with self._lock:
            tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        log_business_event(logger, "Run协调器已关闭")

    async def _dispatch_if_queued(self, run: Run, *, reason: str) -> None:
        if run.status != "queued" or self._closed:
            return
        executor = self._executor
        if executor is None:
            raise RuntimeError("Coordinator 尚未启动")
        async with self._lock:
            existing = self._tasks.get(run.run_id)
            if existing is not None and not existing.done():
                return
            task = asyncio.create_task(
                self._execute(run, executor, reason=reason),
                name=f"run-executor:{run.run_id}",
            )
            self._tasks[run.run_id] = task

    async def _execute(
        self,
        run: Run,
        executor: RunExecutor,
        *,
        reason: str,
    ) -> None:
        started_at = perf_counter()
        log_business_event(
            logger,
            "Run协调器分派开始",
            run_id=run.run_id,
            request_id=run.request_id,
            session_id=run.session_id,
            status=run.status,
            dispatch_reason=reason,
        )
        try:
            await executor(run)
        except asyncio.CancelledError:
            log_business_event(
                logger,
                "Run协调器分派取消",
                run_id=run.run_id,
                session_id=run.session_id,
                dispatch_reason=reason,
                duration_ms=_elapsed_ms(started_at),
            )
            raise
        except Exception as error:
            log_business_event(
                logger,
                "Run协调器分派失败",
                level=logging.ERROR,
                run_id=run.run_id,
                request_id=run.request_id,
                session_id=run.session_id,
                error_code="RUN_EXECUTOR_FAILED",
                error_type=type(error).__name__,
                dispatch_reason=reason,
                duration_ms=_elapsed_ms(started_at),
            )
        else:
            log_business_event(
                logger,
                "Run协调器分派完成",
                run_id=run.run_id,
                request_id=run.request_id,
                session_id=run.session_id,
                dispatch_reason=reason,
                duration_ms=_elapsed_ms(started_at),
            )
        finally:
            async with self._lock:
                current = self._tasks.get(run.run_id)
                if current is asyncio.current_task():
                    del self._tasks[run.run_id]

    async def _scan_loop(self) -> None:
        while True:
            await asyncio.sleep(self._scan_interval_seconds)
            await self.scan_once()


def _elapsed_ms(started_at: float) -> float:
    """返回业务日志使用的毫秒耗时。"""

    return round((perf_counter() - started_at) * 1000, 2)
