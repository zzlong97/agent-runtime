"""S3-08 Capability 单进程并发、超时取消与错误映射测试。"""

import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agent_runtime.capabilities.contracts import CapabilityError
from agent_runtime.capabilities.manifest import (
    ConcurrencyPolicy,
    ExecutionPolicy,
)


class _FakeInvocationRepository:
    """记录 Invocation durable 事件并维护单个 open 调用。"""

    def __init__(self) -> None:
        self.next_seq = 0
        self.events = []
        self.open_event = None

    async def reserve_sequence_block(self, *, run_id, block_size):
        from agent_runtime.runtime.event_models import SequenceBlock

        first = self.next_seq + 1
        self.next_seq += block_size
        return SequenceBlock(first=first, last=self.next_seq)

    async def begin_capability_invocation(self, event):
        from agent_runtime.runtime.event_models import (
            CapabilityInvocationEventCommit,
        )

        self.open_event = event
        self.events.append(event)
        return CapabilityInvocationEventCommit(event=event, recovered=False)

    async def finish_capability_invocation(self, event):
        self.events.append(event)
        self.open_event = None
        return event


def _bounded_policy(
    *,
    max_concurrency: int = 1,
    acquire_timeout_seconds: float = 0.2,
) -> ConcurrencyPolicy:
    return ConcurrencyPolicy(
        mode="bounded",
        max_concurrency=max_concurrency,
        acquire_timeout_seconds=acquire_timeout_seconds,
    )


def _execution_policy(
    *,
    timeout_seconds: float = 0.02,
    cancel_grace_seconds: float = 0.02,
) -> ExecutionPolicy:
    return ExecutionPolicy(
        timeout_seconds=timeout_seconds,
        cancel_grace_seconds=cancel_grace_seconds,
    )


def test_bounded_controller_never_exceeds_declared_capacity() -> None:
    from agent_runtime.capabilities.execution import (
        CapabilityConcurrencyController,
    )

    async def scenario() -> None:
        controller = CapabilityConcurrencyController(
            capability_id="bounded_cap",
            policy=_bounded_policy(max_concurrency=2, acquire_timeout_seconds=1.0),
        )
        first_wave_entered = asyncio.Event()
        release = asyncio.Event()
        active = 0
        maximum = 0
        lock = asyncio.Lock()

        async def worker() -> None:
            nonlocal active, maximum
            async with controller.acquire():
                async with lock:
                    active += 1
                    maximum = max(maximum, active)
                    if active == 2:
                        first_wave_entered.set()
                await release.wait()
                async with lock:
                    active -= 1

        tasks = [asyncio.create_task(worker()) for _ in range(12)]
        await asyncio.wait_for(first_wave_entered.wait(), timeout=1.0)
        await asyncio.sleep(0)
        assert active == 2
        assert maximum == 2
        release.set()
        await asyncio.gather(*tasks)
        assert maximum == 2

    asyncio.run(scenario())


def test_unlimited_controller_does_not_allocate_or_limit_slots() -> None:
    from agent_runtime.capabilities.execution import (
        CapabilityConcurrencyController,
    )

    async def scenario() -> None:
        controller = CapabilityConcurrencyController(
            capability_id="unlimited_cap",
            policy=ConcurrencyPolicy(
                mode="unlimited",
                max_concurrency=None,
                acquire_timeout_seconds=0.01,
            ),
        )
        entered = 0
        all_entered = asyncio.Event()
        release = asyncio.Event()

        async def worker() -> None:
            nonlocal entered
            async with controller.acquire():
                entered += 1
                if entered == 20:
                    all_entered.set()
                await release.wait()

        tasks = [asyncio.create_task(worker()) for _ in range(20)]
        await asyncio.wait_for(all_entered.wait(), timeout=1.0)
        assert controller.is_bounded is False
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())


def test_acquire_timeout_maps_to_busy_and_cancelled_waiter_does_not_leak_slot() -> None:
    from agent_runtime.capabilities.execution import (
        CapabilityConcurrencyController,
    )

    async def scenario() -> None:
        controller = CapabilityConcurrencyController(
            capability_id="busy_cap",
            policy=_bounded_policy(acquire_timeout_seconds=0.01),
        )

        async with controller.acquire():
            with pytest.raises(CapabilityError) as caught:
                async with controller.acquire():
                    raise AssertionError("并发槽超时后不应进入调用体")
            assert caught.value.code == "CAPABILITY_BUSY"
            assert caught.value.retryable is True

            waiter = asyncio.create_task(_enter_once(controller))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter

        await asyncio.wait_for(_enter_once(controller), timeout=0.2)

    asyncio.run(scenario())


async def _enter_once(controller: object) -> None:
    acquire = getattr(controller, "acquire")
    async with acquire():
        return None


def test_concurrency_slot_is_released_for_success_error_and_outer_cancel() -> None:
    from agent_runtime.capabilities.execution import (
        CapabilityConcurrencyController,
    )

    async def scenario() -> None:
        controller = CapabilityConcurrencyController(
            capability_id="release_cap",
            policy=_bounded_policy(acquire_timeout_seconds=0.1),
        )

        async with controller.acquire():
            pass

        with pytest.raises(RuntimeError, match="受控失败"):
            async with controller.acquire():
                raise RuntimeError("受控失败")

        entered = asyncio.Event()

        async def cancelled_holder() -> None:
            async with controller.acquire():
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(cancelled_holder())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await asyncio.wait_for(_enter_once(controller), timeout=0.2)

    asyncio.run(scenario())


def test_execution_timeout_requests_cooperative_cancel_before_returning_timeout() -> None:
    from agent_runtime.capabilities.execution import CapabilityExecutionController

    async def scenario() -> None:
        controller = CapabilityExecutionController(
            capability_id="cooperative_cap",
            policy=_execution_policy(
                timeout_seconds=0.01,
                cancel_grace_seconds=0.2,
            ),
        )
        cooperative_cancel_seen = asyncio.Event()

        async def operation(cancellation):
            while not await cancellation.is_requested():
                await asyncio.sleep(0)
            cooperative_cancel_seen.set()
            return "已协作退出"

        with pytest.raises(CapabilityError) as caught:
            await controller.execute(operation)
        assert caught.value.code == "CAPABILITY_TIMEOUT"
        assert caught.value.retryable is True
        assert cooperative_cancel_seen.is_set()

    asyncio.run(scenario())


def test_execution_timeout_force_cancels_capability_ignoring_cooperative_signal() -> None:
    from agent_runtime.capabilities.execution import CapabilityExecutionController

    async def scenario() -> None:
        controller = CapabilityExecutionController(
            capability_id="stubborn_cap",
            policy=_execution_policy(
                timeout_seconds=0.01,
                cancel_grace_seconds=0.01,
            ),
        )
        started = asyncio.Event()
        force_cancel_seen = asyncio.Event()

        async def operation(_cancellation):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                force_cancel_seen.set()

        with pytest.raises(CapabilityError) as caught:
            await controller.execute(operation)
        assert caught.value.code == "CAPABILITY_TIMEOUT"
        assert started.is_set()
        assert force_cancel_seen.is_set()

    asyncio.run(scenario())


def test_explicit_outer_cancel_is_not_mapped_to_timeout_and_still_cleans_child() -> None:
    from agent_runtime.capabilities.execution import CapabilityExecutionController

    async def scenario() -> None:
        controller = CapabilityExecutionController(
            capability_id="cancel_cap",
            policy=_execution_policy(
                timeout_seconds=10.0,
                cancel_grace_seconds=0.1,
            ),
        )
        started = asyncio.Event()
        cooperative_cancel_seen = asyncio.Event()

        async def operation(cancellation):
            started.set()
            while not await cancellation.is_requested():
                await asyncio.sleep(0)
            cooperative_cancel_seen.set()
            return "显式取消后退出"

        task = asyncio.create_task(controller.execute(operation))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert cooperative_cancel_seen.is_set()

    asyncio.run(scenario())


def test_upstream_cancel_probe_wins_before_deadline_even_when_capability_ignores_it() -> None:
    from agent_runtime.capabilities.execution import CapabilityExecutionController
    from agent_runtime.runtime.agent_contract import AgentCancellation

    async def scenario() -> None:
        controller = CapabilityExecutionController(
            capability_id="probe_cancel_cap",
            policy=_execution_policy(
                timeout_seconds=0.2,
                cancel_grace_seconds=0.01,
            ),
        )
        cancel_requested = asyncio.Event()
        child_started = asyncio.Event()
        child_finished = asyncio.Event()

        async def probe() -> bool:
            return cancel_requested.is_set()

        async def operation(_cancellation):
            child_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                child_finished.set()

        task = asyncio.create_task(
            controller.execute(
                operation,
                upstream_cancellation=AgentCancellation(probe),
            )
        )
        await child_started.wait()
        cancel_requested.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.15)
        assert child_finished.is_set()

    asyncio.run(scenario())


def test_repeated_outer_cancel_cannot_interrupt_child_cleanup() -> None:
    from agent_runtime.capabilities.execution import CapabilityExecutionController

    async def scenario() -> None:
        controller = CapabilityExecutionController(
            capability_id="repeated_cancel_cap",
            policy=_execution_policy(
                timeout_seconds=10.0,
                cancel_grace_seconds=0.05,
            ),
        )
        child_started = asyncio.Event()
        child_finished = asyncio.Event()

        async def operation(_cancellation):
            child_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                child_finished.set()

        task = asyncio.create_task(controller.execute(operation))
        await child_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
        assert child_finished.is_set()
        assert not any(
            pending.get_name() == "capability-invocation:repeated_cancel_cap"
            for pending in asyncio.all_tasks()
            if pending is not asyncio.current_task()
        )

    asyncio.run(scenario())


def test_explicit_cancel_composes_to_cancelled_invocation_not_timeout() -> None:
    from agent_runtime.capabilities.execution import CapabilityExecutionController
    from agent_runtime.capabilities.invocation_service import (
        CapabilityInvocationService,
    )
    from agent_runtime.runtime.agent_contract import AgentCancellation
    from agent_runtime.runtime.sequencer import RunSequencer

    async def scenario() -> None:
        repository = _FakeInvocationRepository()
        invocation_service = CapabilityInvocationService(
            sequencer=RunSequencer.for_run(
                run_id=uuid4(),
                response_message_id=uuid4(),
                repository=repository,
                block_size=2,
            )
        )
        controller = CapabilityExecutionController(
            capability_id="cancel_cap",
            policy=_execution_policy(
                timeout_seconds=10.0,
                cancel_grace_seconds=0.1,
            ),
        )
        child_started = asyncio.Event()
        cancel_requested = asyncio.Event()

        async def probe() -> bool:
            return cancel_requested.is_set()

        async def operation(cancellation):
            child_started.set()
            while not await cancellation.is_requested():
                await asyncio.sleep(0)
            return "已取消"

        async def invoke() -> None:
            invocation = await invocation_service.start(
                capability_id="cancel_cap",
                task_action="new",
                state_scope="invocation",
                created_at=datetime.now(UTC),
            )
            try:
                await controller.execute(
                    operation,
                    upstream_cancellation=AgentCancellation(probe),
                )
            except asyncio.CancelledError:
                await invocation_service.cancel(
                    invocation_id=invocation.invocation_id,
                    capability_id=invocation.capability_id,
                    task_id=None,
                    requested_task_action=invocation.task_action,
                    effective_task_action=None,
                    state_scope=invocation.state_scope,
                    state_schema_version=None,
                    created_at=datetime.now(UTC),
                )
                raise

        task = asyncio.create_task(invoke())
        await child_started.wait()
        cancel_requested.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.2)

        assert [event.event_type for event in repository.events] == [
            "internal.capability.invocation.started",
            "internal.capability.invocation.cancelled",
        ]
        assert all(
            event.payload.get("error_code") != "CAPABILITY_TIMEOUT"
            for event in repository.events
        )

    asyncio.run(scenario())


def test_completion_wins_before_deadline_without_requesting_cancel() -> None:
    from agent_runtime.capabilities.execution import CapabilityExecutionController

    async def scenario() -> None:
        controller = CapabilityExecutionController(
            capability_id="fast_cap",
            policy=_execution_policy(timeout_seconds=1.0),
        )
        cancellation_seen = None

        async def operation(cancellation):
            nonlocal cancellation_seen
            cancellation_seen = await cancellation.is_requested()
            return "完成"

        assert await controller.execute(operation) == "完成"
        assert cancellation_seen is False

    asyncio.run(scenario())


def test_explicit_cancel_wins_when_it_arrives_during_timeout_grace() -> None:
    from agent_runtime.capabilities.execution import CapabilityExecutionController

    async def scenario() -> None:
        controller = CapabilityExecutionController(
            capability_id="race_cap",
            policy=_execution_policy(
                timeout_seconds=0.01,
                cancel_grace_seconds=0.02,
            ),
        )
        timeout_signal_seen = asyncio.Event()
        child_finished = asyncio.Event()

        async def operation(cancellation):
            try:
                while not await cancellation.is_requested():
                    await asyncio.sleep(0)
                timeout_signal_seen.set()
                await asyncio.Event().wait()
            finally:
                child_finished.set()

        task = asyncio.create_task(controller.execute(operation))
        await asyncio.wait_for(timeout_signal_seen.wait(), timeout=0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert child_finished.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("code", "expected_retryable"),
    [
        ("CAPABILITY_PERMISSION_DENIED", False),
        ("CAPABILITY_BUSY", True),
        ("CAPABILITY_TIMEOUT", True),
        ("CAPABILITY_UNAVAILABLE", True),
        ("CAPABILITY_STATE_VERSION_INCOMPATIBLE", False),
        ("CAPABILITY_REGENERATE_UNSUPPORTED", False),
        ("CAPABILITY_MANUAL_RECOVERY_REQUIRED", False),
        ("CAPABILITY_EXECUTION_FAILED", False),
    ],
)
def test_error_mapper_whitelists_stable_codes_and_returns_nonempty_public_message(
    code: str,
    expected_retryable: bool,
) -> None:
    from agent_runtime.capabilities.execution import CapabilityErrorMapper

    mapped = CapabilityErrorMapper().map(
        CapabilityError(
            code=code,
            message="内部错误消息不得公开",
            retryable=expected_retryable,
            details={"private": "内部详情不得公开"},
        )
    )

    assert mapped.code == code
    assert mapped.retryable is expected_retryable
    assert mapped.message.strip()
    assert "内部错误消息" not in mapped.message
    assert "内部详情" not in mapped.message


@pytest.mark.parametrize(
    ("code", "incorrect_retryable", "expected_retryable"),
    [
        ("CAPABILITY_PERMISSION_DENIED", True, False),
        ("CAPABILITY_BUSY", False, True),
        ("CAPABILITY_TIMEOUT", False, True),
        ("CAPABILITY_UNAVAILABLE", False, True),
        ("CAPABILITY_STATE_VERSION_INCOMPATIBLE", True, False),
        ("CAPABILITY_REGENERATE_UNSUPPORTED", True, False),
        ("CAPABILITY_MANUAL_RECOVERY_REQUIRED", True, False),
        ("CAPABILITY_EXECUTION_FAILED", True, False),
    ],
)
def test_error_mapper_uses_runtime_retryability_not_capability_claim(
    code: str,
    incorrect_retryable: bool,
    expected_retryable: bool,
) -> None:
    from agent_runtime.capabilities.execution import CapabilityErrorMapper

    mapped = CapabilityErrorMapper().map(
        CapabilityError(
            code=code,
            message="内部消息",
            retryable=incorrect_retryable,
        )
    )
    assert mapped.retryable is expected_retryable


def test_error_mapper_converges_unknown_errors_without_logging_private_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from agent_runtime.capabilities.execution import CapabilityErrorMapper

    secret_message = "secret-exception-message"
    secret_detail = "secret-detail-value"
    mapper = CapabilityErrorMapper()

    with caplog.at_level(logging.INFO):
        unknown_code = mapper.map(
            CapabilityError(
                code="PRIVATE_PROVIDER_FAILURE",
                message=secret_message,
                retryable=True,
                details={"secret": secret_detail},
            )
        )
        unknown_exception = mapper.map(RuntimeError(secret_message))

    for mapped in (unknown_code, unknown_exception):
        assert mapped.code == "CAPABILITY_EXECUTION_FAILED"
        assert mapped.retryable is False
        assert mapped.message == "能力执行失败，请稍后重试"
    assert secret_message not in caplog.text
    assert secret_detail not in caplog.text


def test_error_mapper_never_converts_explicit_cancellation_to_failure() -> None:
    from agent_runtime.capabilities.execution import CapabilityErrorMapper

    with pytest.raises(asyncio.CancelledError):
        CapabilityErrorMapper().map(asyncio.CancelledError())


def test_busy_path_can_close_started_invocation_without_resolving_task() -> None:
    from agent_runtime.capabilities.execution import (
        CapabilityConcurrencyController,
    )
    from agent_runtime.capabilities.invocation_service import (
        CapabilityInvocationService,
    )
    from agent_runtime.runtime.sequencer import RunSequencer

    async def scenario() -> None:
        controller = CapabilityConcurrencyController(
            capability_id="busy_cap",
            policy=_bounded_policy(acquire_timeout_seconds=0.01),
        )
        repository = _FakeInvocationRepository()
        sequencer = RunSequencer.for_run(
            run_id=uuid4(),
            response_message_id=uuid4(),
            repository=repository,
            block_size=2,
        )
        invocation_service = CapabilityInvocationService(sequencer=sequencer)
        task_resolve_calls = 0

        async def blocked_invocation() -> None:
            nonlocal task_resolve_calls
            invocation = await invocation_service.start(
                capability_id="busy_cap",
                task_action="new",
                state_scope="session",
                created_at=datetime.now(UTC),
            )
            try:
                async with controller.acquire():
                    task_resolve_calls += 1
            except CapabilityError as error:
                await invocation_service.fail(
                    invocation_id=invocation.invocation_id,
                    capability_id=invocation.capability_id,
                    task_id=None,
                    requested_task_action=invocation.task_action,
                    effective_task_action=None,
                    state_scope=invocation.state_scope,
                    state_schema_version=None,
                    error_code=error.code,
                    created_at=datetime.now(UTC),
                )

        async with controller.acquire():
            await blocked_invocation()

        assert task_resolve_calls == 0
        assert [event.event_type for event in repository.events] == [
            "internal.capability.invocation.started",
            "internal.capability.invocation.failed",
        ]
        assert repository.events[-1].payload["task_id"] is None
        assert repository.events[-1].payload["error_code"] == "CAPABILITY_BUSY"

    asyncio.run(scenario())


def test_registry_active_entry_owns_one_concurrency_controller() -> None:
    from agent_runtime.capabilities.execution import (
        CapabilityConcurrencyController,
    )
    from agent_runtime.capabilities.registry import CapabilityRegistryEntry

    assert "concurrency_controller" in CapabilityRegistryEntry.__dataclass_fields__
    field = CapabilityRegistryEntry.__dataclass_fields__["concurrency_controller"]
    assert field is not None
