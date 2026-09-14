"""Stage 2 单实例 Active Run 协调与产品事件队列。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID, uuid4

from agent_runtime.api.schemas.chat import (
    DoneEventData,
    ErrorEventData,
    MessageEventData,
)
from agent_runtime.core.errors import ApplicationError

type CancelReason = Literal["stopped", "disconnected"]
type ProductEventName = Literal["message", "error", "done"]
type ProductEventData = MessageEventData | ErrorEventData | DoneEventData


class SessionBusyError(ApplicationError):
    """同一 Session 已经存在活动 Run。"""


@dataclass(frozen=True, slots=True)
class ProductRunEvent:
    """producer 与 SSE Adapter 之间传递的已校验产品事件。"""

    name: ProductEventName
    data: ProductEventData


@dataclass(slots=True)
class ActiveRun:
    """一次活动执行的进程内控制句柄，不保存 Parent 或 Child 业务状态。"""

    session_id: UUID
    response_message_id: UUID
    producer_task: asyncio.Task[None] | None = None
    cancel_reason: CancelReason | None = None
    terminal_future: asyncio.Future[None] = field(init=False)
    event_queue: asyncio.Queue[ProductRunEvent | None] = field(
        default_factory=asyncio.Queue
    )
    _reservation_token: UUID = field(default_factory=uuid4, repr=False)
    _finalizing: bool = field(default=False, repr=False)
    _terminal_error: ApplicationError | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """把终态 Future 绑定到创建本 Run 的当前事件循环。"""

        self.terminal_future = asyncio.get_running_loop().create_future()


class ActiveRunRegistry:
    """保证单个应用进程内每个 Session 同时最多存在一个 Run。"""

    def __init__(self) -> None:
        self._active_runs: dict[UUID, ActiveRun] = {}
        self._lock = asyncio.Lock()

    async def reserve(
        self,
        session_id: UUID,
        response_message_id: UUID,
    ) -> ActiveRun:
        """在新增 HumanMessage 和建立 SSE 前原子占用 Session。"""

        async with self._lock:
            if session_id in self._active_runs:
                raise SessionBusyError(
                    code="SESSION_BUSY",
                    message="当前 Session 正在生成回复",
                    status_code=409,
                    retryable=True,
                )
            run = ActiveRun(
                session_id=session_id,
                response_message_id=response_message_id,
            )
            self._active_runs[session_id] = run
            return run

    async def attach_producer(
        self,
        run: ActiveRun,
        task: asyncio.Task[None],
    ) -> bool:
        """绑定 producer；已失效的旧 Run 不得启动或影响新 Run。"""

        async with self._lock:
            current = self._active_runs.get(run.session_id)
            if (
                current is not run
                or current._reservation_token != run._reservation_token
            ):
                task.cancel()
                return False
            if run.producer_task is not None:
                task.cancel()
                if run.cancel_reason is not None:
                    return False
                raise RuntimeError("同一 Active Run 不能重复绑定 producer")
            run.producer_task = task
            return True

    async def get_active(self, session_id: UUID) -> ActiveRun | None:
        """返回当前 Session 的活动 Run 快照；调用方不得据此取消替代 Run。"""

        async with self._lock:
            return self._active_runs.get(session_id)

    async def begin_finalization(self, run: ActiveRun) -> bool:
        """原子冻结当前 Run 的终态，阻止后到的 Stop 改写已确定结果。"""

        async with self._lock:
            current = self._active_runs.get(run.session_id)
            if (
                current is not run
                or current._reservation_token != run._reservation_token
                or run._finalizing
            ):
                return False
            run._finalizing = True
            return True

    async def request_cancel(
        self,
        run: ActiveRun,
        reason: CancelReason,
        *,
        unstarted_finalizer: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """只取消与响应句柄完全匹配的 Run，并等待其完成清理。"""

        async with self._lock:
            current = self._active_runs.get(run.session_id)
            if (
                current is not run
                or current._reservation_token != run._reservation_token
            ):
                return False
            if run._finalizing:
                task = None
                accepted = run.cancel_reason is not None
                wait_for_terminal = True
            else:
                accepted = True
                wait_for_terminal = False
                should_cancel_task = run.cancel_reason is None
                if should_cancel_task:
                    run.cancel_reason = reason
                task = run.producer_task
                if task is None:
                    if unstarted_finalizer is None:
                        del self._active_runs[run.session_id]
                        run.event_queue.put_nowait(None)
                        if not run.terminal_future.done():
                            run.terminal_future.set_result(None)
                    else:
                        task = asyncio.create_task(
                            unstarted_finalizer(),
                            name=f"run-finalizer:{run.session_id}",
                        )
                        run.producer_task = task
                        should_cancel_task = False

        if not wait_for_terminal and task is not None and should_cancel_task:
            task.cancel()
        await asyncio.shield(run.terminal_future)
        return accepted

    async def release(self, run: ActiveRun) -> None:
        """仅由当前 reservation token 释放 Session，忽略过期 Run。"""

        async with self._lock:
            current = self._active_runs.get(run.session_id)
            if (
                current is run
                and current._reservation_token == run._reservation_token
            ):
                del self._active_runs[run.session_id]
            if not run.terminal_future.done():
                run.terminal_future.set_result(None)

    async def close(
        self,
        *,
        unstarted_finalizer: Callable[[ActiveRun], Awaitable[None]] | None = None,
    ) -> None:
        """取消并等待 Registry 中仍在运行的全部 producer。"""

        async with self._lock:
            runs = tuple(self._active_runs.values())
        await asyncio.gather(
            *(
                self.request_cancel(
                    run,
                    "disconnected",
                    unstarted_finalizer=(
                        None
                        if unstarted_finalizer is None
                        else lambda run=run: unstarted_finalizer(run)
                    ),
                )
                for run in runs
            )
        )
