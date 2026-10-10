"""Stage 3 Operation Ledger 的稳定幂等键与受控异步上下文。"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import Literal, Protocol
from uuid import UUID, uuid4

from agent_runtime.capabilities.persistence import CapabilityOperationCreateResult
from agent_runtime.capabilities.persistence_models import CapabilityOperation
from agent_runtime.core.logging import log_business_event

logger = logging.getLogger(__name__)

_CANONICAL_VERSION = b"agent-runtime:capability-operation:v1"


class CapabilityOperationStore(Protocol):
    """Operation Context 所需的最小持久化接口。"""

    async def create_or_get_for_invocation(
        self,
        operation: CapabilityOperation,
    ) -> CapabilityOperationCreateResult:
        """校验调用归属后创建或复用 pending Operation。"""

        ...

    async def transition_pending(
        self,
        *,
        operation_id: UUID,
        status: Literal["succeeded", "failed"],
        updated_at: datetime,
    ) -> CapabilityOperation:
        """只允许 pending 原子进入指定终态。"""

        ...


def build_operation_idempotency_key(
    *,
    run_id: UUID,
    invocation_id: UUID,
    operation_key: str,
) -> str:
    """按固定字段顺序和长度前缀生成小写 SHA-256 幂等键。"""

    _validate_operation_key(operation_key)
    parts = (
        _CANONICAL_VERSION,
        str(run_id).encode("utf-8"),
        str(invocation_id).encode("utf-8"),
        operation_key.encode("utf-8"),
    )
    canonical = b"".join(
        len(part).to_bytes(4, byteorder="big") + part for part in parts
    )
    return hashlib.sha256(canonical).hexdigest()


class CapabilityOperationHandle:
    """向 Capability 暴露是否执行、副作用幂等键和确定失败标记。"""

    __slots__ = ("_context", "_operation", "_should_execute")

    def __init__(
        self,
        *,
        context: CapabilityOperationContext,
        operation: CapabilityOperation,
        should_execute: bool,
    ) -> None:
        self._context = context
        self._operation = operation
        self._should_execute = should_execute

    @property
    def operation_id(self) -> UUID:
        """返回服务端分配且重复进入时保持稳定的 Operation ID。"""

        return self._operation.operation_id

    @property
    def idempotency_key(self) -> str:
        """返回应传给外部幂等接口的稳定 SHA-256 键。"""

        return self._operation.idempotency_key

    @property
    def should_execute(self) -> bool:
        """表示本次是否允许实际调用外部副作用。"""

        return self._should_execute

    async def mark_failed(self) -> None:
        """仅在 Capability 已确认业务失败时把 pending 标记为 failed。"""

        await self._context._mark_failed()

    def _replace_operation(self, operation: CapabilityOperation) -> None:
        """同步 Context 已提交的最新持久状态。"""

        self._operation = operation


class CapabilityOperationContext:
    """单个业务副作用的异步 Context Manager。"""

    __slots__ = (
        "_gateway",
        "_operation_key",
        "_entered",
        "_handle",
        "_marked_failed",
    )

    def __init__(
        self,
        *,
        gateway: CapabilityOperationGateway,
        operation_key: str,
    ) -> None:
        _validate_operation_key(operation_key)
        self._gateway = gateway
        self._operation_key = operation_key
        self._entered = False
        self._handle: CapabilityOperationHandle | None = None
        self._marked_failed = False

    async def __aenter__(self) -> CapabilityOperationHandle:
        """原子创建或读取 Ledger，并按既有终态决定是否执行。"""

        if self._entered:
            raise RuntimeError("Capability Operation Context 只能进入一次")
        self._entered = True
        started_at = perf_counter()
        now = self._gateway._now_factory()
        proposed = CapabilityOperation(
            operation_id=self._gateway._operation_id_factory(),
            run_id=self._gateway.run_id,
            invocation_id=self._gateway.invocation_id,
            task_id=self._gateway.task_id,
            capability_id=self._gateway.capability_id,
            operation_key=self._operation_key,
            idempotency_key=self._gateway.idempotency_key(self._operation_key),
            status="pending",
            created_at=now,
            updated_at=now,
        )
        result = await self._gateway._repository.create_or_get_for_invocation(
            proposed
        )
        operation = result.operation
        if operation.status == "failed":
            log_business_event(
                logger,
                "Capability Operation自动重试拒绝",
                operation_id=operation.operation_id,
                run_id=operation.run_id,
                invocation_id=operation.invocation_id,
                task_id=operation.task_id,
                capability_id=operation.capability_id,
                status=operation.status,
                error_code="CAPABILITY_OPERATION_FAILED",
                duration_ms=_elapsed_ms(started_at),
            )
            from agent_runtime.capabilities.contracts import CapabilityError

            raise CapabilityError(
                code="CAPABILITY_OPERATION_FAILED",
                message="Capability Operation 已确定失败，不允许自动重试",
                retryable=False,
            )
        handle = CapabilityOperationHandle(
            context=self,
            operation=operation,
            should_execute=operation.status == "pending",
        )
        self._handle = handle
        log_business_event(
            logger,
            "Capability Operation上下文进入",
            operation_id=operation.operation_id,
            run_id=operation.run_id,
            invocation_id=operation.invocation_id,
            task_id=operation.task_id,
            capability_id=operation.capability_id,
            status=operation.status,
            should_execute=handle.should_execute,
            duration_ms=_elapsed_ms(started_at),
        )
        return handle

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> Literal[False]:
        """正常退出标记成功；异常结果未知时保持 pending。"""

        del exc_value, traceback
        handle = self._handle
        if handle is None:
            return False
        if (
            exc_type is None
            and handle.should_execute
            and not self._marked_failed
        ):
            updated = await self._gateway._repository.transition_pending(
                operation_id=handle.operation_id,
                status="succeeded",
                updated_at=self._gateway._now_factory(),
            )
            handle._replace_operation(updated)
        log_business_event(
            logger,
            "Capability Operation上下文退出",
            operation_id=handle.operation_id,
            run_id=self._gateway.run_id,
            invocation_id=self._gateway.invocation_id,
            task_id=self._gateway.task_id,
            capability_id=self._gateway.capability_id,
            status=(
                "failed"
                if self._marked_failed
                else "succeeded"
                if exc_type is None and handle.should_execute
                else "pending"
                if handle.should_execute
                else "succeeded"
            ),
            exit_reason="normal" if exc_type is None else "exception",
        )
        return False

    async def _mark_failed(self) -> None:
        """由 Handle 调用并幂等提交确定失败终态。"""

        handle = self._handle
        if handle is None:
            raise RuntimeError("Capability Operation Context 尚未进入")
        if not handle.should_execute:
            from agent_runtime.capabilities.contracts import CapabilityError

            raise CapabilityError(
                code="CAPABILITY_OPERATION_ALREADY_SUCCEEDED",
                message="已成功的 Capability Operation 不能标记失败",
                retryable=False,
            )
        if self._marked_failed:
            return
        updated = await self._gateway._repository.transition_pending(
            operation_id=handle.operation_id,
            status="failed",
            updated_at=self._gateway._now_factory(),
        )
        handle._replace_operation(updated)
        self._marked_failed = True


class CapabilityOperationGateway:
    """把当前 Run、Invocation、Task 与 Operation Repository 绑定为受控入口。"""

    __slots__ = (
        "run_id",
        "invocation_id",
        "task_id",
        "capability_id",
        "_repository",
        "_operation_id_factory",
        "_now_factory",
    )

    def __init__(
        self,
        *,
        run_id: UUID,
        invocation_id: UUID,
        task_id: UUID,
        capability_id: str,
        repository: CapabilityOperationStore,
        operation_id_factory: Callable[[], UUID] = uuid4,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.run_id = run_id
        self.invocation_id = invocation_id
        self.task_id = task_id
        self.capability_id = capability_id
        self._repository = repository
        self._operation_id_factory = operation_id_factory
        self._now_factory = now_factory or (lambda: datetime.now(UTC))

    def idempotency_key(self, operation_key: str) -> str:
        """生成绑定当前 Run 与 Invocation 的稳定幂等键。"""

        return build_operation_idempotency_key(
            run_id=self.run_id,
            invocation_id=self.invocation_id,
            operation_key=operation_key,
        )

    def operation(self, operation_key: str) -> CapabilityOperationContext:
        """创建一个单次使用的异步 Operation Context。"""

        return CapabilityOperationContext(
            gateway=self,
            operation_key=operation_key,
        )


def _validate_operation_key(operation_key: str) -> None:
    """拒绝空白、NUL 和非字符串业务操作键，不改写 Capability 原值。"""

    if (
        type(operation_key) is not str
        or not operation_key.strip()
        or "\x00" in operation_key
    ):
        raise ValueError("Operation Key 必须是非空且不含 NUL 的字符串")


def _elapsed_ms(started_at: float) -> float:
    """返回保留两位小数的毫秒耗时。"""

    return round((perf_counter() - started_at) * 1000, 2)
