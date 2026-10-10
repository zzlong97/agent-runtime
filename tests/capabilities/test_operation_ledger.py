"""S3-09 Operation Ledger 与受控执行上下文单元测试。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from agent_runtime.capabilities.persistence import CapabilityOperationCreateResult
from agent_runtime.capabilities.persistence_models import CapabilityOperation


RUN_ID = UUID("00000000-0000-0000-0000-000000000901")
INVOCATION_ID = UUID("00000000-0000-0000-0000-000000000902")
TASK_ID = UUID("00000000-0000-0000-0000-000000000903")
OPERATION_ID = UUID("00000000-0000-0000-0000-000000000904")
NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


class InMemoryOperationRepository:
    """仅模拟 Ledger 持久状态，不模拟 Capability 业务调用。"""

    def __init__(self) -> None:
        self.operations: dict[str, CapabilityOperation] = {}
        self.transitions: list[tuple[UUID, str]] = []

    async def create_or_get_for_invocation(
        self,
        operation: CapabilityOperation,
    ) -> CapabilityOperationCreateResult:
        existing = self.operations.get(operation.idempotency_key)
        if existing is not None:
            return CapabilityOperationCreateResult(
                operation=existing,
                created=False,
            )
        self.operations[operation.idempotency_key] = operation
        return CapabilityOperationCreateResult(operation=operation, created=True)

    async def transition_pending(
        self,
        *,
        operation_id: UUID,
        status: str,
        updated_at: datetime,
    ) -> CapabilityOperation:
        operation = next(
            value
            for value in self.operations.values()
            if value.operation_id == operation_id
        )
        if operation.status == status:
            return operation
        if operation.status != "pending":
            raise RuntimeError("Operation 已进入其他终态")
        updated = replace(operation, status=status, updated_at=updated_at)
        self.operations[operation.idempotency_key] = updated
        self.transitions.append((operation_id, status))
        return updated


def _gateway(repository: InMemoryOperationRepository):
    from agent_runtime.capabilities.operation_ledger import (
        CapabilityOperationGateway,
    )

    return CapabilityOperationGateway(
        run_id=RUN_ID,
        invocation_id=INVOCATION_ID,
        task_id=TASK_ID,
        capability_id="general_chat",
        repository=repository,
        operation_id_factory=lambda: OPERATION_ID,
        now_factory=lambda: NOW,
    )


def _run_context(gateway=None):
    from agent_runtime.runtime.agent_contract import (
        AgentCancellation,
        AgentEventOutlet,
        RunContext,
    )

    async def never_cancelled() -> bool:
        return False

    return RunContext(
        run_id=RUN_ID,
        request_id=None,
        input_message_id=None,
        response_message_id=UUID(
            "00000000-0000-0000-0000-000000000905"
        ),
        cancellation=AgentCancellation(never_cancelled),
        events=AgentEventOutlet(lambda _event: None),
        invocation_id=INVOCATION_ID,
        operation_gateway=gateway,
    )


def test_idempotency_key_uses_locked_canonical_vector_and_all_dimensions() -> None:
    from agent_runtime.capabilities.operation_ledger import (
        build_operation_idempotency_key,
    )

    expected = "16c05a019491df546687da457c31ab1506d269b53f73db5fa0d6fd18debc1e7e"
    assert (
        build_operation_idempotency_key(
            run_id=RUN_ID,
            invocation_id=INVOCATION_ID,
            operation_key="发送邮件",
        )
        == expected
    )
    assert len(
        {
            build_operation_idempotency_key(
                run_id=RUN_ID,
                invocation_id=INVOCATION_ID,
                operation_key="发送邮件",
            ),
            build_operation_idempotency_key(
                run_id=UUID(int=RUN_ID.int + 1),
                invocation_id=INVOCATION_ID,
                operation_key="发送邮件",
            ),
            build_operation_idempotency_key(
                run_id=RUN_ID,
                invocation_id=UUID(int=INVOCATION_ID.int + 1),
                operation_key="发送邮件",
            ),
            build_operation_idempotency_key(
                run_id=RUN_ID,
                invocation_id=INVOCATION_ID,
                operation_key="发送短信",
            ),
        }
    ) == 4


@pytest.mark.parametrize("operation_key", ["", "   ", "\x00"])
def test_idempotency_key_rejects_invalid_business_key(operation_key: str) -> None:
    from agent_runtime.capabilities.operation_ledger import (
        build_operation_idempotency_key,
    )

    with pytest.raises(ValueError, match="Operation Key"):
        build_operation_idempotency_key(
            run_id=RUN_ID,
            invocation_id=INVOCATION_ID,
            operation_key=operation_key,
        )


def test_normal_exit_marks_pending_succeeded_and_prevents_duplicate_call() -> None:
    repository = InMemoryOperationRepository()
    context = _run_context(_gateway(repository))
    provider_calls: list[str] = []

    async def exercise() -> None:
        async with context.operation("send-email") as handle:
            assert handle.should_execute is True
            assert handle.idempotency_key == context.idempotency_key("send-email")
            provider_calls.append(handle.idempotency_key)

        async with context.operation("send-email") as repeated:
            assert repeated.operation_id == OPERATION_ID
            assert repeated.should_execute is False
            if repeated.should_execute:
                provider_calls.append(repeated.idempotency_key)

    asyncio.run(exercise())

    stored = next(iter(repository.operations.values()))
    assert stored.status == "succeeded"
    assert repository.transitions == [(OPERATION_ID, "succeeded")]
    assert provider_calls == [stored.idempotency_key]


def test_unknown_exception_keeps_pending_and_reuses_same_identity() -> None:
    repository = InMemoryOperationRepository()
    context = _run_context(_gateway(repository))

    async def exercise() -> tuple[UUID, UUID]:
        first_id = None
        with pytest.raises(LookupError, match="结果未知"):
            async with context.operation("charge") as handle:
                first_id = handle.operation_id
                raise LookupError("结果未知")
        assert next(iter(repository.operations.values())).status == "pending"
        async with context.operation("charge") as repeated:
            assert repeated.should_execute is True
            return first_id, repeated.operation_id

    first_id, repeated_id = asyncio.run(exercise())

    assert first_id == repeated_id == OPERATION_ID
    assert next(iter(repository.operations.values())).status == "succeeded"


def test_explicit_mark_failed_is_terminal_and_rejects_automatic_retry() -> None:
    from agent_runtime.capabilities.contracts import CapabilityError

    repository = InMemoryOperationRepository()
    context = _run_context(_gateway(repository))

    async def exercise() -> None:
        async with context.operation("reserve") as handle:
            await handle.mark_failed()
            await handle.mark_failed()
        assert next(iter(repository.operations.values())).status == "failed"

        with pytest.raises(CapabilityError) as caught:
            async with context.operation("reserve"):
                raise AssertionError("failed Operation 不得进入业务代码")
        assert caught.value.code == "CAPABILITY_OPERATION_FAILED"
        assert caught.value.retryable is False

    asyncio.run(exercise())
    assert repository.transitions == [(OPERATION_ID, "failed")]


def test_multiple_operations_of_one_invocation_are_independent() -> None:
    repository = InMemoryOperationRepository()
    next_operation_id = iter(
        (
            OPERATION_ID,
            UUID("00000000-0000-0000-0000-000000000906"),
        )
    )
    gateway = _gateway(repository)
    object.__setattr__(
        gateway,
        "_operation_id_factory",
        lambda: next(next_operation_id),
    )
    context = _run_context(gateway)

    async def exercise() -> None:
        async with context.operation("first") as first:
            assert first.should_execute is True
        with pytest.raises(RuntimeError, match="第二个操作失败"):
            async with context.operation("second") as second:
                assert second.should_execute is True
                raise RuntimeError("第二个操作失败")

    asyncio.run(exercise())
    statuses = {
        operation.operation_key: operation.status
        for operation in repository.operations.values()
    }
    assert statuses == {"first": "succeeded", "second": "pending"}


def test_run_context_rejects_missing_or_mismatched_operation_gateway() -> None:
    from agent_runtime.runtime.agent_contract import RunContext

    without_gateway = _run_context()
    with pytest.raises(RuntimeError, match="Operation Ledger"):
        without_gateway.idempotency_key("send-email")
    with pytest.raises(RuntimeError, match="Operation Ledger"):
        without_gateway.operation("send-email")

    repository = InMemoryOperationRepository()
    gateway = _gateway(repository)
    object.__setattr__(gateway, "invocation_id", UUID(int=INVOCATION_ID.int + 1))
    with pytest.raises(ValueError, match="Invocation"):
        RunContext(
            run_id=RUN_ID,
            request_id=None,
            input_message_id=None,
            response_message_id=UUID(int=1),
            cancellation=without_gateway.cancellation,
            events=without_gateway.events,
            invocation_id=INVOCATION_ID,
            operation_gateway=gateway,
        )


def test_operation_key_and_exception_text_never_enter_business_logs(caplog) -> None:
    repository = InMemoryOperationRepository()
    context = _run_context(_gateway(repository))

    async def exercise() -> None:
        with pytest.raises(RuntimeError):
            async with context.operation("secret-customer-order"):
                raise RuntimeError("secret-provider-response")

    with caplog.at_level(
        logging.INFO,
        logger="agent_runtime.capabilities.operation_ledger",
    ):
        asyncio.run(exercise())

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "Capability Operation上下文进入" in rendered
    assert "Capability Operation上下文退出" in rendered
    assert "secret-customer-order" not in rendered
    assert "secret-provider-response" not in rendered
