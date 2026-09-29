"""Stage 2.5 单进程 Run 唤醒与 queued 补偿协调。"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import UUID
from weakref import WeakValueDictionary

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
        cancel_grace_seconds: float = 2.0,
    ) -> None:
        if scan_interval_seconds <= 0:
            raise ValueError("Coordinator 扫描间隔必须大于 0 秒")
        if cancel_grace_seconds <= 0:
            raise ValueError("Run 取消宽限时间必须大于 0 秒")
        self._repository = repository
        self._user_id = user_id
        self._scan_interval_seconds = scan_interval_seconds
        self._cancel_grace_seconds = cancel_grace_seconds
        self._executor: RunExecutor | None = None
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._cancel_signals: dict[UUID, asyncio.Event] = {}
        self._force_cancel_tasks: dict[UUID, asyncio.Task[None]] = {}
        self._claim_locks: WeakValueDictionary[UUID, asyncio.Lock] = (
            WeakValueDictionary()
        )
        self._finalizing_runs: set[UUID] = set()
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
        await self._dispatch_if_actionable(run, reason="submit")

    async def wake_resumed(self, run_id: UUID) -> None:
        """只为当前进程刚提交成功的 Resume 分派 running Run。"""

        if self._closed:
            return
        try:
            run = await self._repository.get(run_id)
        except Exception as error:
            log_business_event(
                logger,
                "Run协调器恢复唤醒失败",
                level=logging.ERROR,
                run_id=run_id,
                error_code="RUN_COORDINATOR_RESUME_WAKE_FAILED",
                error_type=type(error).__name__,
            )
            raise
        await self._dispatch_if_actionable(
            run,
            reason="resume",
            allowed_statuses=frozenset({"running"}),
        )

    async def scan_once(self, *, reason: str = "compensation") -> None:
        """扫描固定用户的非终态 Run，并分派 queued 或取消投影任务。"""

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
        cancel_requested_count = 0
        deferred_count = 0
        for run in runs:
            if run.status == "queued":
                queued_count += 1
                await self._dispatch_if_actionable(run, reason=reason)
            elif run.status == "cancel_requested":
                cancel_requested_count += 1
                await self._dispatch_if_actionable(run, reason=reason)
            else:
                # running/recovering 的对账恢复属于 S2.5-08。
                deferred_count += 1
        log_business_event(
            logger,
            "Run协调器扫描完成",
            scan_reason=reason,
            active_count=len(runs),
            queued_count=queued_count,
            cancel_requested_count=cancel_requested_count,
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
            pending = asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.shield(pending)

    async def wait_cancel_requested(self, run_id: UUID) -> None:
        """等待指定 Run 的进程内协作式取消信号。"""

        async with self._lock:
            signal = self._cancel_signals.setdefault(run_id, asyncio.Event())
        await signal.wait()

    @asynccontextmanager
    async def claim_guard(self, run_id: UUID) -> AsyncIterator[None]:
        """串行化同进程内 queued 领取与直接取消的临界区。"""

        async with self._lock:
            guard = self._claim_locks.setdefault(run_id, asyncio.Lock())
        async with guard:
            yield

    async def is_cancel_requested(self, run_id: UUID) -> bool:
        """读取指定 Run 是否已收到进程内协作式取消信号。"""

        async with self._lock:
            signal = self._cancel_signals.get(run_id)
            return signal is not None and signal.is_set()

    async def request_cancel(self, run_id: UUID) -> None:
        """先通知执行器协作退出，并安排宽限期后的本地任务强制取消。"""

        async with self._lock:
            signal = self._cancel_signals.setdefault(run_id, asyncio.Event())
            signal.set()
            current = self._tasks.get(run_id)
            existing_force = self._force_cancel_tasks.get(run_id)
            if (
                run_id in self._finalizing_runs
                or current is None
                or current.done()
                or (existing_force is not None and not existing_force.done())
            ):
                return
            force_task = asyncio.create_task(
                self._force_cancel_after_grace(run_id, current),
                name=f"run-force-cancel:{run_id}",
            )
            self._force_cancel_tasks[run_id] = force_task
        log_business_event(
            logger,
            "Run协调器取消信号已发送",
            run_id=run_id,
            cancel_grace_seconds=self._cancel_grace_seconds,
        )

    async def begin_cancel_finalization(self, run_id: UUID) -> None:
        """标记执行器已响应取消，并停止可能打断终态落盘的宽限计时。"""

        async with self._lock:
            self._finalizing_runs.add(run_id)
            force_task = self._force_cancel_tasks.pop(run_id, None)
            if force_task is not None and force_task is not asyncio.current_task():
                force_task.cancel()
        log_business_event(logger, "Run协调器取消终结开始", run_id=run_id)

    async def wait_for_run(self, run_id: UUID) -> None:
        """等待指定 Run 当前进程内执行任务结束；不存在任务时立即返回。"""

        async with self._lock:
            task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.shield(task)

    async def close(self) -> None:
        """停止扫描并取消本地任务，持久 Run 留给后续恢复。"""

        self._closed = True
        scan_task = self._scan_task
        self._scan_task = None
        if scan_task is not None:
            scan_task.cancel()
            await asyncio.gather(scan_task, return_exceptions=True)
        async with self._lock:
            task_entries = tuple(self._tasks.items())
            force_cancel_tasks = tuple(self._force_cancel_tasks.values())
        for run_id, task in task_entries:
            if run_id not in self._finalizing_runs:
                task.cancel()
        if task_entries:
            pending = asyncio.gather(
                *(task for _run_id, task in task_entries),
                return_exceptions=True,
            )
            await asyncio.shield(pending)
        for task in force_cancel_tasks:
            task.cancel()
        if force_cancel_tasks:
            await asyncio.gather(*force_cancel_tasks, return_exceptions=True)
        log_business_event(logger, "Run协调器已关闭")

    async def _dispatch_if_actionable(
        self,
        run: Run,
        *,
        reason: str,
        allowed_statuses: frozenset[str] = frozenset(
            {"queued", "cancel_requested"}
        ),
    ) -> None:
        if run.status not in allowed_statuses or self._closed:
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

    async def _force_cancel_after_grace(
        self,
        run_id: UUID,
        expected_task: asyncio.Task[None],
    ) -> None:
        """宽限期后只取消仍与 Run 绑定的同一进程内任务。"""

        try:
            await asyncio.sleep(self._cancel_grace_seconds)
            async with self._lock:
                current = self._tasks.get(run_id)
                if current is expected_task and not current.done():
                    current.cancel()
                    log_business_event(
                        logger,
                        "Run协调器执行强制取消",
                        run_id=run_id,
                    )
        finally:
            async with self._lock:
                if self._force_cancel_tasks.get(run_id) is asyncio.current_task():
                    del self._force_cancel_tasks[run_id]

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
                self._cancel_signals.pop(run.run_id, None)
                self._finalizing_runs.discard(run.run_id)
                force_task = self._force_cancel_tasks.pop(run.run_id, None)
                if force_task is not None and force_task is not asyncio.current_task():
                    force_task.cancel()

    async def _scan_loop(self) -> None:
        while True:
            await asyncio.sleep(self._scan_interval_seconds)
            await self.scan_once()


def _elapsed_ms(started_at: float) -> float:
    """返回业务日志使用的毫秒耗时。"""

    return round((perf_counter() - started_at) * 1000, 2)
