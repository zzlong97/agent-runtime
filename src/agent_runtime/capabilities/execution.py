"""Capability 单进程并发、两阶段超时取消与安全错误映射。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter
from typing import TypeVar

from agent_runtime.capabilities.contracts import CapabilityError
from agent_runtime.capabilities.manifest import (
    ConcurrencyPolicy,
    ExecutionPolicy,
    validate_capability_id,
)
from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event
from agent_runtime.runtime.agent_contract import AgentCancellation

logger = logging.getLogger(__name__)

_ResultT = TypeVar("_ResultT")
type CapabilityOperation[_ResultT] = Callable[
    [AgentCancellation],
    Awaitable[_ResultT],
]

_PUBLIC_ERRORS: dict[str, tuple[str, bool]] = {
    "CAPABILITY_PERMISSION_DENIED": ("当前用户未获授权使用该能力", False),
    "CAPABILITY_BUSY": ("该能力当前繁忙，请稍后重试", True),
    "CAPABILITY_TIMEOUT": ("该能力执行超时，请稍后重试", True),
    "CAPABILITY_UNAVAILABLE": ("该能力当前不可用，请稍后重试", True),
    "CAPABILITY_STATE_VERSION_INCOMPATIBLE": (
        "该能力的任务状态版本不兼容",
        False,
    ),
    "CAPABILITY_REGENERATE_UNSUPPORTED": ("该能力不支持重新生成", False),
    "CAPABILITY_MANUAL_RECOVERY_REQUIRED": (
        "该能力需要人工恢复后才能继续",
        False,
    ),
    "CAPABILITY_EXECUTION_FAILED": ("能力执行失败，请稍后重试", False),
}


class CapabilityConcurrencyController:
    """按单个 Manifest 的固定策略治理当前进程内并发槽。"""

    def __init__(
        self,
        *,
        capability_id: str,
        policy: ConcurrencyPolicy,
    ) -> None:
        if not isinstance(policy, ConcurrencyPolicy):
            raise TypeError("Capability 并发控制器必须使用已校验的并发策略")
        self._capability_id = validate_capability_id(capability_id)
        self._policy = policy
        self._semaphore = (
            asyncio.Semaphore(policy.max_concurrency)
            if policy.mode == "bounded" and policy.max_concurrency is not None
            else None
        )

    @property
    def is_bounded(self) -> bool:
        """返回当前控制器是否启用固定容量的单进程限制。"""

        return self._semaphore is not None

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        """在有限时间内获取槽位，并仅从统一 finally 路径释放一次。"""

        semaphore = self._semaphore
        if semaphore is None:
            yield
            return

        started_at = perf_counter()
        acquired = False
        log_business_event(
            logger,
            "Capability并发槽等待开始",
            capability_id=self._capability_id,
            concurrency_mode=self._policy.mode,
            max_concurrency=self._policy.max_concurrency,
        )
        try:
            try:
                async with asyncio.timeout(
                    self._policy.acquire_timeout_seconds
                ):
                    await semaphore.acquire()
                acquired = True
            except TimeoutError as error:
                log_business_event(
                    logger,
                    "Capability并发槽等待超时",
                    level=logging.WARNING,
                    capability_id=self._capability_id,
                    status="busy",
                    error_code="CAPABILITY_BUSY",
                    duration_ms=_elapsed_ms(started_at),
                )
                raise CapabilityError(
                    code="CAPABILITY_BUSY",
                    message="等待 Capability 并发槽超时",
                    retryable=True,
                ) from error

            log_business_event(
                logger,
                "Capability并发槽获取完成",
                capability_id=self._capability_id,
                status="acquired",
                duration_ms=_elapsed_ms(started_at),
            )
            yield
        finally:
            if acquired:
                semaphore.release()
                log_business_event(
                    logger,
                    "Capability并发槽释放完成",
                    capability_id=self._capability_id,
                    status="released",
                )


class CapabilityExecutionController:
    """为单次 Invocation 执行两阶段超时取消并保留显式取消语义。"""

    def __init__(
        self,
        *,
        capability_id: str,
        policy: ExecutionPolicy,
    ) -> None:
        if not isinstance(policy, ExecutionPolicy):
            raise TypeError("Capability 执行控制器必须使用已校验的执行策略")
        self._capability_id = validate_capability_id(capability_id)
        self._policy = policy

    async def execute(
        self,
        operation: CapabilityOperation[_ResultT],
        *,
        upstream_cancellation: AgentCancellation | None = None,
    ) -> _ResultT:
        """执行受控调用；超时先通知协作退出，宽限期后才强制取消。"""

        if not callable(operation):
            raise TypeError("Capability operation 必须可调用")
        if await _is_cancellation_requested(upstream_cancellation):
            raise asyncio.CancelledError
        local_cancel_requested = asyncio.Event()

        async def cancellation_probe() -> bool:
            if local_cancel_requested.is_set():
                return True
            if upstream_cancellation is None:
                return False
            return await upstream_cancellation.is_requested()

        cancellation = AgentCancellation(cancellation_probe)
        started_at = perf_counter()
        task = asyncio.create_task(
            operation(cancellation),
            name=f"capability-invocation:{self._capability_id}",
        )
        deadline_task = asyncio.create_task(
            asyncio.sleep(self._policy.timeout_seconds),
            name=f"capability-deadline:{self._capability_id}",
        )
        upstream_cancel_task = (
            asyncio.create_task(
                _wait_for_cancellation(upstream_cancellation),
                name=f"capability-upstream-cancel:{self._capability_id}",
            )
            if upstream_cancellation is not None
            else None
        )
        monitor_tasks = tuple(
            monitor
            for monitor in (deadline_task, upstream_cancel_task)
            if monitor is not None
        )
        log_business_event(
            logger,
            "Capability受控执行开始",
            capability_id=self._capability_id,
        )
        try:
            done, _ = await asyncio.wait(
                {task, *monitor_tasks},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if upstream_cancel_task in done:
                upstream_cancel_task.result()
                raise asyncio.CancelledError
            if await _is_cancellation_requested(upstream_cancellation):
                raise asyncio.CancelledError
            if task in done:
                result = task.result()
                log_business_event(
                    logger,
                    "Capability受控执行完成",
                    capability_id=self._capability_id,
                    status="completed",
                    duration_ms=_elapsed_ms(started_at),
                )
                return result

            local_cancel_requested.set()
            log_business_event(
                logger,
                "Capability执行超时请求协作取消",
                level=logging.WARNING,
                capability_id=self._capability_id,
                status="timeout_cancelling",
                error_code="CAPABILITY_TIMEOUT",
                duration_ms=_elapsed_ms(started_at),
            )
            forced = await _finish_child_task(
                task,
                grace_seconds=self._policy.cancel_grace_seconds,
            )
            if await _is_cancellation_requested(upstream_cancellation):
                raise asyncio.CancelledError
            log_business_event(
                logger,
                "Capability执行超时收尾完成",
                level=logging.WARNING,
                capability_id=self._capability_id,
                status="force_cancelled" if forced else "cooperative_cancelled",
                error_code="CAPABILITY_TIMEOUT",
                duration_ms=_elapsed_ms(started_at),
            )
            raise CapabilityError(
                code="CAPABILITY_TIMEOUT",
                message="Capability 执行超过 Manifest 声明时限",
                retryable=True,
            )
        except asyncio.CancelledError:
            local_cancel_requested.set()
            forced = await _finish_child_task_resilient(
                task,
                grace_seconds=self._policy.cancel_grace_seconds,
                task_name=f"capability-cancel-cleanup:{self._capability_id}",
            )
            log_business_event(
                logger,
                "Capability显式取消收尾完成",
                level=logging.INFO,
                capability_id=self._capability_id,
                status="force_cancelled" if forced else "cooperative_cancelled",
                duration_ms=_elapsed_ms(started_at),
            )
            raise
        except Exception:
            local_cancel_requested.set()
            await _finish_child_task_resilient(
                task,
                grace_seconds=self._policy.cancel_grace_seconds,
                task_name=f"capability-error-cleanup:{self._capability_id}",
            )
            raise
        finally:
            _stop_monitor_tasks(monitor_tasks)


class CapabilityErrorMapper:
    """把 Capability 失败收敛为不含内部详情的稳定 Run 级错误。"""

    def map(self, error: BaseException) -> ApplicationError:
        """只保留白名单错误码和 retryable，未知错误统一安全收敛。"""

        if isinstance(error, asyncio.CancelledError):
            raise error
        if isinstance(error, CapabilityError) and error.code in _PUBLIC_ERRORS:
            code = error.code
        else:
            code = "CAPABILITY_EXECUTION_FAILED"
        message, retryable = _PUBLIC_ERRORS[code]

        mapped = ApplicationError(
            code=code,
            message=message,
            retryable=retryable,
        )
        log_business_event(
            logger,
            "Capability错误映射完成",
            level=logging.WARNING,
            error_code=mapped.code,
            error_type=type(error).__name__,
            retryable=mapped.retryable,
        )
        return mapped


async def _finish_child_task(
    task: asyncio.Task[object],
    *,
    grace_seconds: float,
) -> bool:
    """等待协作退出；仍运行时强制取消，并消费全部子任务终态。"""

    if not task.done() and grace_seconds > 0:
        done, _ = await asyncio.wait({task}, timeout=grace_seconds)
        if task in done:
            _consume_task_result(task)
            return False
    if task.done():
        _consume_task_result(task)
        return False

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return True


async def _finish_child_task_resilient(
    task: asyncio.Task[object],
    *,
    grace_seconds: float,
    task_name: str,
) -> bool:
    """屏蔽重复外层取消，直到 Capability 子任务已完成本地收尾。"""

    cleanup = asyncio.create_task(
        _finish_child_task(task, grace_seconds=grace_seconds),
        name=task_name,
    )
    while True:
        try:
            return await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            if cleanup.done():
                return cleanup.result()
            continue


async def _is_cancellation_requested(
    cancellation: AgentCancellation | None,
) -> bool:
    """读取上游显式取消状态；未提供上游探针时固定返回 false。"""

    return cancellation is not None and await cancellation.is_requested()


async def _wait_for_cancellation(cancellation: AgentCancellation) -> None:
    """轮询只读探针，使显式取消能独立参与执行 deadline 竞争。"""

    while not await cancellation.is_requested():
        await asyncio.sleep(0.01)


def _stop_monitor_tasks(tasks: tuple[asyncio.Task[object], ...]) -> None:
    """停止 deadline 与上游取消观察任务，并消费其最终异常。"""

    for task in tasks:
        if not task.done():
            task.cancel()
        task.add_done_callback(_consume_task_result)


def _consume_task_result(task: asyncio.Task[object]) -> None:
    """消费协作退出时的结果或异常，避免泄漏私有异常到事件循环日志。"""

    try:
        task.result()
    except BaseException:
        return


def _elapsed_ms(started_at: float) -> float:
    """返回适合中文业务日志的毫秒耗时。"""

    return round((perf_counter() - started_at) * 1000, 2)
