"""S3-09 Operation Ledger 的真实 PostgreSQL 集成测试。"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest


def run_on_psycopg_compatible_loop(coroutine):
    """使用 psycopg 在 Windows 下兼容的事件循环执行协程。"""

    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coroutine)


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_TESTS") != "1",
        reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
    ),
]


@dataclass(slots=True)
class OperationCase:
    settings: object
    session_id: UUID
    submission: object
    invocation: object
    resolution: object
    operation_repository: object
    event_repository: object


def _manifest():
    from agent_runtime.capabilities.manifest import CapabilityManifest

    return CapabilityManifest.model_validate(
        {
            "manifest_schema_version": 1,
            "capability_id": "general_chat",
            "name": "Operation 测试能力",
            "description": "仅验证 Operation Ledger，不执行真实外部副作用。",
            "enabled": True,
            "entrypoint": "tests.fake_capability:create_capability",
            "version": "1.0.0",
            "state_scope": "session",
            "state_schema_version": "v1",
            "compatible_state_schema_versions": [],
            "allow_degraded": False,
            "concurrency": {
                "mode": "unlimited",
                "max_concurrency": None,
                "acquire_timeout_seconds": 1.0,
            },
            "execution": {
                "timeout_seconds": 30.0,
                "cancel_grace_seconds": 1.0,
            },
            "recovery_policy": "automatic",
            "side_effect_policy": "idempotent",
        }
    )


async def _prepare_case(label: str) -> OperationCase:
    from agent_runtime.capabilities.invocation_service import (
        CapabilityInvocationService,
    )
    from agent_runtime.capabilities.persistence import (
        CapabilityOperationRepository,
        CapabilityTaskContextRepository,
        CapabilityTaskRepository,
    )
    from agent_runtime.capabilities.task_service import CapabilityTaskService
    from agent_runtime.core.config import Settings
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.models import RunSubmission
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.runtime.sequencer import RunSequencer
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s3-09-{label}-{uuid4()}",
        _env_file=None,
    )
    now = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)
    session_id = uuid4()
    payload = {"message": {"content": "S3-09 集成测试输入"}}
    submission = RunSubmission(
        run_id=uuid4(),
        request_id=uuid4(),
        session_id=session_id,
        thread_id=str(session_id),
        parent_run_id=None,
        run_type="normal",
        input_message_id=uuid4(),
        response_message_id=uuid4(),
        start_checkpoint_id=None,
        input_payload=payload,
        request_fingerprint=build_request_fingerprint(
            run_type="normal",
            session_id=session_id,
            request_payload=payload,
        ),
        created_at=now,
    )
    session_repository = PostgresSessionRepository(settings)
    run_repository = PostgresRunRepository(settings)
    event_repository = PostgresRuntimeEventRepository(settings)
    task_repository = CapabilityTaskRepository(settings)
    context_repository = CapabilityTaskContextRepository(settings)
    operation_repository = CapabilityOperationRepository(settings)
    await session_repository.setup()
    await run_repository.setup()
    await event_repository.setup()
    await task_repository.setup()
    await context_repository.setup()
    await operation_repository.setup()
    await session_repository.add(
        Session(
            session_id=session_id,
            user_id=settings.local_user_id,
            title="S3-09 Operation Ledger",
            created_at=now,
            updated_at=now,
        )
    )
    await run_repository.create_or_get(submission)
    sequencer = RunSequencer.for_run(
        run_id=submission.run_id,
        response_message_id=submission.response_message_id,
        repository=event_repository,
        block_size=16,
    )
    invocation_service = CapabilityInvocationService(sequencer=sequencer)
    invocation = await invocation_service.start(
        capability_id="general_chat",
        task_action="new",
        state_scope="session",
        created_at=now + timedelta(seconds=1),
    )
    task_service = CapabilityTaskService(
        repository=task_repository,
        sequencer=sequencer,
        invocation_service=invocation_service,
    )
    resolution = await task_service.resolve(
        manifest=_manifest(),
        session_id=session_id,
        run_id=submission.run_id,
        invocation_id=invocation.invocation_id,
        task_action="new",
        updated_at=now + timedelta(seconds=2),
    )
    return OperationCase(
        settings=settings,
        session_id=session_id,
        submission=submission,
        invocation=invocation,
        resolution=resolution,
        operation_repository=operation_repository,
        event_repository=event_repository,
    )


async def _cleanup_case(case: OperationCase) -> None:
    from agent_runtime.persistence.database import open_database_connection

    async with open_database_connection(case.settings) as connection:
        await connection.execute(
            "DELETE FROM capability_task_contexts WHERE session_id = %s",
            (case.session_id,),
        )
        await connection.execute(
            "DELETE FROM capability_operations WHERE run_id = %s",
            (case.submission.run_id,),
        )
        await connection.execute(
            "DELETE FROM capability_tasks WHERE session_id = %s",
            (case.session_id,),
        )
        await connection.execute(
            "DELETE FROM runtime_events WHERE run_id = %s",
            (case.submission.run_id,),
        )
        await connection.execute(
            "DELETE FROM runs WHERE run_id = %s",
            (case.submission.run_id,),
        )
        await connection.execute(
            "DELETE FROM sessions WHERE session_id = %s",
            (case.session_id,),
        )
        await connection.commit()


def test_real_ledger_serializes_get_or_create_and_reuses_pending_after_crash() -> None:
    from agent_runtime.capabilities.operation_ledger import (
        CapabilityOperationGateway,
        build_operation_idempotency_key,
    )
    from agent_runtime.capabilities.persistence_models import CapabilityOperation
    from agent_runtime.persistence.database import open_database_connection

    async def exercise() -> None:
        case = await _prepare_case("concurrency")
        now = datetime(2026, 10, 10, 10, 5, tzinfo=UTC)
        try:
            idempotency_key = build_operation_idempotency_key(
                run_id=case.submission.run_id,
                invocation_id=case.invocation.invocation_id,
                operation_key="concurrent-send",
            )

            def candidate() -> CapabilityOperation:
                return CapabilityOperation(
                    operation_id=uuid4(),
                    run_id=case.submission.run_id,
                    invocation_id=case.invocation.invocation_id,
                    task_id=case.resolution.task.task_id,
                    capability_id="general_chat",
                    operation_key="concurrent-send",
                    idempotency_key=idempotency_key,
                    status="pending",
                    created_at=now,
                    updated_at=now,
                )

            results = await asyncio.gather(
                *(
                    case.operation_repository.create_or_get_for_invocation(
                        candidate()
                    )
                    for _ in range(8)
                )
            )
            assert sum(result.created for result in results) == 1
            assert len({result.operation.operation_id for result in results}) == 1

            provider_attempts = 0
            provider_executions: set[str] = set()
            provider_execution_count = 0

            async def provider_call(key: str) -> None:
                nonlocal provider_attempts, provider_execution_count
                provider_attempts += 1
                if key in provider_executions:
                    return
                provider_executions.add(key)
                provider_execution_count += 1

            gateway = CapabilityOperationGateway(
                run_id=case.submission.run_id,
                invocation_id=case.invocation.invocation_id,
                task_id=case.resolution.task.task_id,
                capability_id="general_chat",
                repository=case.operation_repository,
                now_factory=lambda: now,
            )
            crashed_context = gateway.operation("provider-charge")
            crashed_handle = await crashed_context.__aenter__()
            assert crashed_handle.should_execute is True
            await provider_call(crashed_handle.idempotency_key)

            restarted_gateway = CapabilityOperationGateway(
                run_id=case.submission.run_id,
                invocation_id=case.invocation.invocation_id,
                task_id=case.resolution.task.task_id,
                capability_id="general_chat",
                repository=case.operation_repository,
                now_factory=lambda: now + timedelta(seconds=1),
            )
            async with restarted_gateway.operation("provider-charge") as resumed:
                assert resumed.operation_id == crashed_handle.operation_id
                assert resumed.should_execute is True
                await provider_call(resumed.idempotency_key)
            async with restarted_gateway.operation("provider-charge") as completed:
                assert completed.should_execute is False

            assert provider_attempts == 2
            assert provider_execution_count == 1
            assert len(provider_executions) == 1
            async with open_database_connection(case.settings) as connection:
                cursor = await connection.execute(
                    """
                    SELECT operation_key, status, COUNT(*) OVER () AS total
                    FROM capability_operations
                    WHERE run_id = %s
                    ORDER BY operation_key
                    """,
                    (case.submission.run_id,),
                )
                rows = await cursor.fetchall()
            assert [(row["operation_key"], row["status"]) for row in rows] == [
                ("concurrent-send", "pending"),
                ("provider-charge", "succeeded"),
            ]
            assert all(row["total"] == 2 for row in rows)
        finally:
            await _cleanup_case(case)

    run_on_psycopg_compatible_loop(exercise())


def test_real_ledger_rejects_wrong_ownership_and_allows_only_one_terminal(
    caplog,
) -> None:
    from agent_runtime.capabilities.operation_ledger import (
        build_operation_idempotency_key,
    )
    from agent_runtime.capabilities.persistence import (
        CapabilityPersistenceConflictError,
    )
    from agent_runtime.capabilities.persistence_models import CapabilityOperation
    from agent_runtime.persistence.database import open_database_connection

    async def exercise() -> None:
        case = await _prepare_case("ownership")
        now = datetime(2026, 10, 10, 10, 10, tzinfo=UTC)
        try:
            valid = CapabilityOperation(
                operation_id=uuid4(),
                run_id=case.submission.run_id,
                invocation_id=case.invocation.invocation_id,
                task_id=case.resolution.task.task_id,
                capability_id="general_chat",
                operation_key="terminal-race",
                idempotency_key=build_operation_idempotency_key(
                    run_id=case.submission.run_id,
                    invocation_id=case.invocation.invocation_id,
                    operation_key="terminal-race",
                ),
                status="pending",
                created_at=now,
                updated_at=now,
            )
            wrong_invocation = uuid4()
            with pytest.raises(Exception) as invocation_error:
                await case.operation_repository.create_or_get_for_invocation(
                    replace(
                        valid,
                        operation_id=uuid4(),
                        invocation_id=wrong_invocation,
                        operation_key="wrong-invocation",
                        idempotency_key=build_operation_idempotency_key(
                            run_id=case.submission.run_id,
                            invocation_id=wrong_invocation,
                            operation_key="wrong-invocation",
                        ),
                    )
                )
            assert getattr(invocation_error.value, "code", None) == (
                "CAPABILITY_INVOCATION_MISMATCH"
            )

            with pytest.raises(CapabilityPersistenceConflictError) as task_error:
                await case.operation_repository.create_or_get_for_invocation(
                    replace(
                        valid,
                        operation_id=uuid4(),
                        task_id=uuid4(),
                        operation_key="wrong-task",
                        idempotency_key=build_operation_idempotency_key(
                            run_id=case.submission.run_id,
                            invocation_id=case.invocation.invocation_id,
                            operation_key="wrong-task",
                        ),
                    )
                )
            assert task_error.value.code == "CAPABILITY_OPERATION_OWNERSHIP_CONFLICT"

            created = await case.operation_repository.create_or_get_for_invocation(
                valid
            )
            with caplog.at_level(
                logging.INFO,
                logger="agent_runtime.capabilities.persistence",
            ):
                outcomes = await asyncio.gather(
                    case.operation_repository.transition_pending(
                        operation_id=created.operation.operation_id,
                        status="succeeded",
                        updated_at=now + timedelta(seconds=1),
                    ),
                    case.operation_repository.transition_pending(
                        operation_id=created.operation.operation_id,
                        status="failed",
                        updated_at=now + timedelta(seconds=1),
                    ),
                    return_exceptions=True,
                )
            completed = [value for value in outcomes if not isinstance(value, Exception)]
            rejected = [value for value in outcomes if isinstance(value, Exception)]
            assert len(completed) == 1
            assert len(rejected) == 1
            winner = completed[0]
            assert winner.status in {"succeeded", "failed"}
            assert isinstance(rejected[0], CapabilityPersistenceConflictError)
            rendered = "\n".join(
                record.getMessage() for record in caplog.records
            )
            assert "Capability Operation状态写入拒绝" in rendered

            repeated = await case.operation_repository.transition_pending(
                operation_id=winner.operation_id,
                status=winner.status,
                updated_at=now + timedelta(seconds=2),
            )
            assert repeated == winner
            async with open_database_connection(case.settings) as connection:
                cursor = await connection.execute(
                    """
                    SELECT status, COUNT(*) OVER () AS total
                    FROM capability_operations
                    WHERE run_id = %s
                    """,
                    (case.submission.run_id,),
                )
                rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0]["status"] == winner.status
            assert rows[0]["total"] == 1
            assert not hasattr(winner, "response_payload")
            assert not hasattr(winner, "error_detail")
        finally:
            await _cleanup_case(case)

    run_on_psycopg_compatible_loop(exercise())
