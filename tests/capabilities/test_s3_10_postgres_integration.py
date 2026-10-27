"""S3-10 Capability 恢复策略的真实 PostgreSQL 集成测试。"""

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_TESTS") != "1",
        reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
    ),
]


def run_on_psycopg_compatible_loop(coroutine):
    """使用 psycopg 在 Windows 下兼容的事件循环执行协程。"""

    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coroutine)


@dataclass(slots=True)
class _RecoveryCase:
    settings: object
    session_id: UUID
    submission: object
    invocation: object
    resolution: object
    operation_repository: object


def _recovery_manifest(
    *,
    capability_id: str = "general_chat",
    recovery_policy: str = "automatic",
    side_effect_policy: str = "idempotent",
):
    """构造用于崩溃恢复矩阵的严格测试 Manifest。"""

    from agent_runtime.capabilities.manifest import CapabilityManifest

    return CapabilityManifest.model_validate(
        {
            "manifest_schema_version": 1,
            "capability_id": capability_id,
            "name": "恢复测试能力",
            "description": "仅验证恢复身份和 Operation Ledger。",
            "enabled": True,
            "transport": {"type": "local", "entrypoint": "tests.fake_capability:create_capability"},
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
            "recovery_policy": recovery_policy,
            "side_effect_policy": side_effect_policy,
        }
    )


async def _prepare_case(
    label: str,
    *,
    manifest=None,
) -> _RecoveryCase:
    """创建一个具有 open Invocation 和 active Task 的真实持久化场景。"""

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
        local_user_id=f"s3-10-{label}-{uuid4()}",
        _env_file=None,
    )
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    session_id = uuid4()
    payload = {"message": {"content": "S3-10 集成测试输入"}}
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
    sessions = PostgresSessionRepository(settings)
    runs = PostgresRunRepository(settings)
    events = PostgresRuntimeEventRepository(settings)
    tasks = CapabilityTaskRepository(settings)
    contexts = CapabilityTaskContextRepository(settings)
    operations = CapabilityOperationRepository(settings)
    await sessions.setup()
    await runs.setup()
    await events.setup()
    await tasks.setup()
    await contexts.setup()
    await operations.setup()
    await sessions.add(
        Session(
            session_id=session_id,
            user_id=settings.local_user_id,
            title="S3-10 恢复测试",
            created_at=now,
            updated_at=now,
        )
    )
    await runs.create_or_get(submission)
    sequencer = RunSequencer.for_run(
        run_id=submission.run_id,
        response_message_id=submission.response_message_id,
        repository=events,
    )
    invocation_service = CapabilityInvocationService(sequencer=sequencer)
    selected_manifest = manifest or _recovery_manifest()
    invocation = await invocation_service.start(
        capability_id=selected_manifest.capability_id,
        task_action="new",
        state_scope="session",
        created_at=now + timedelta(seconds=1),
    )
    task_service = CapabilityTaskService(
        repository=tasks,
        sequencer=sequencer,
        invocation_service=invocation_service,
    )
    resolution = await task_service.resolve(
        manifest=selected_manifest,
        session_id=session_id,
        run_id=submission.run_id,
        invocation_id=invocation.invocation_id,
        task_action="new",
        updated_at=now + timedelta(seconds=2),
    )
    return _RecoveryCase(
        settings=settings,
        session_id=session_id,
        submission=submission,
        invocation=invocation,
        resolution=resolution,
        operation_repository=operations,
    )


async def _cleanup_case(case: _RecoveryCase) -> None:
    """按外键顺序删除当前测试创建的全部业务记录。"""

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


class _Registry:
    """只向恢复策略提供单个已校验 Manifest 的静态测试 Registry。"""

    def __init__(self, manifest) -> None:
        self._manifest = manifest

    def get(self, capability_id: str):
        if capability_id != self._manifest.capability_id:
            return None
        return SimpleNamespace(manifest=self._manifest)


class _EmptyRegistry:
    """模拟重启后目标 Capability 未进入静态 Registry。"""

    @staticmethod
    def get(_capability_id: str):
        return None


def test_recovery_policy_error_claims_attempt_and_terminally_fails() -> None:
    """确定性策略错误必须消费恢复次数并释放 Session，不得留下活动 Run。"""

    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, START, StateGraph

    from agent_runtime.capabilities.persistence import (
        CapabilityOperationRepository,
        CapabilityTaskRepository,
    )
    from agent_runtime.capabilities.recovery import (
        CapabilityRecoveryPolicyEvaluator,
        OpenInvocationResolver,
    )
    from agent_runtime.chat import ChatService
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.state import ParentState
    from agent_runtime.runtime.coordinator import RunCoordinator
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )
    from agent_runtime.runtime.event_schemas import (
        MessageStartedPayload,
        RunStartedPayload,
    )
    from agent_runtime.runtime.interrupts import (
        PostgresRunInterruptRepository,
    )
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.runtime.sequencer import RunSequencer
    from agent_runtime.sessions.repository import PostgresSessionRepository
    from agent_runtime.sessions.service import SessionService

    graph_calls = 0

    def should_not_run(_state):
        nonlocal graph_calls
        graph_calls += 1
        raise AssertionError("恢复策略拒绝后不得执行 Graph")

    async def exercise() -> None:
        case = await _prepare_case("policy-error")
        runs = PostgresRunRepository(case.settings)
        events = PostgresRuntimeEventRepository(case.settings)
        sessions = PostgresSessionRepository(case.settings)
        interrupts = PostgresRunInterruptRepository(case.settings)
        await interrupts.setup()
        sequencer = RunSequencer.for_run(
            run_id=case.submission.run_id,
            response_message_id=case.submission.response_message_id,
            repository=events,
        )
        await sequencer.transition_run(
            target_status="running",
            event=RuntimeEventDraft(
                event_type="run.started",
                source="runtime.executor",
                visibility="public",
                payload=RunStartedPayload(status="running"),
                schema_version=1,
                durability="durable",
                created_at=datetime.now(UTC),
            ),
            updated_at=datetime.now(UTC),
        )
        await sequencer.emit(
            RuntimeEventDraft(
                event_type="message.started",
                source="runtime.executor",
                visibility="public",
                payload=MessageStartedPayload(
                    response_message_id=case.submission.response_message_id,
                    attempt=1,
                ),
                schema_version=1,
                durability="durable",
                created_at=datetime.now(UTC),
            )
        )
        resolver = OpenInvocationResolver(
            event_repository=events,
            task_repository=CapabilityTaskRepository(case.settings),
            operation_repository=CapabilityOperationRepository(case.settings),
        )
        evaluator = CapabilityRecoveryPolicyEvaluator(
            registry=_EmptyRegistry(),
            invocation_resolver=resolver,
        )
        try:
            async with AsyncPostgresSaver.from_conn_string(
                case.settings.database_connection_string
            ) as saver:
                await saver.setup()
                builder = StateGraph(ParentState)
                builder.add_node("invoke_capability", should_not_run)
                builder.add_edge(START, "invoke_capability")
                builder.add_edge("invoke_capability", END)
                graph = builder.compile(checkpointer=saver)
                coordinator = RunCoordinator(
                    repository=runs,
                    user_id=case.settings.local_user_id,
                    scan_interval_seconds=60,
                )
                service = ChatService(
                    settings=case.settings,
                    session_repository=sessions,
                    session_service=SessionService(
                        settings=case.settings,
                        session_repository=sessions,
                    ),
                    parent_graph=graph,
                    persistent_run_repository=runs,
                    runtime_event_repository=events,
                    run_interrupt_repository=interrupts,
                    run_coordinator=coordinator,
                    capability_recovery_policy_evaluator=evaluator,
                )
                try:
                    await coordinator.start(service.execute_persistent_run)
                    await coordinator.wait_until_idle()
                    failed = await runs.get(case.submission.run_id)
                    public_events = await events.list_public(
                        run_id=case.submission.run_id,
                        after_seq=0,
                        limit=100,
                    )
                    snapshot = await graph.aget_state(
                        parent_thread_config(case.session_id)
                    )
                    replies = [
                        message
                        for message in snapshot.values["messages"]
                        if isinstance(message, AIMessage)
                        and str(message.id)
                        == str(case.submission.response_message_id)
                    ]
                    open_invocation = (
                        await events.get_open_capability_invocation(
                            run_id=case.submission.run_id
                        )
                    )

                    assert graph_calls == 0
                    assert failed.status == "failed"
                    assert failed.recovery_attempts == 1
                    assert failed.error_code == "CAPABILITY_EXECUTION_FAILED"
                    assert open_invocation is None
                    assert len(replies) == 1
                    assert str(replies[0].text).strip()
                    assert replies[0].additional_kwargs["runtime_status"] == (
                        "incomplete"
                    )
                    assert [
                        event.event_type
                        for event in public_events
                        if event.event_type
                        in {"run.completed", "run.cancelled", "run.failed"}
                    ] == ["run.failed"]
                finally:
                    await coordinator.close()
                    await saver.adelete_thread(str(case.session_id))
        finally:
            await _cleanup_case(case)

    run_on_psycopg_compatible_loop(exercise())


def test_restart_resolves_same_invocation_task_and_pending_operation() -> None:
    """重建全部 Repository 后仍应解析出同一调用和同一幂等键。"""

    from agent_runtime.capabilities.operation_ledger import (
        CapabilityOperationGateway,
    )
    from agent_runtime.capabilities.persistence import (
        CapabilityOperationRepository,
        CapabilityTaskRepository,
    )
    from agent_runtime.capabilities.recovery import OpenInvocationResolver
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )

    async def exercise() -> None:
        case = await _prepare_case("restart-resolver")
        external_attempts = 0
        external_effects: set[str] = set()

        async def call_external(idempotency_key: str) -> None:
            nonlocal external_attempts
            external_attempts += 1
            external_effects.add(idempotency_key)

        try:
            first_gateway = CapabilityOperationGateway(
                run_id=case.submission.run_id,
                invocation_id=case.invocation.invocation_id,
                task_id=case.resolution.task.task_id,
                capability_id="general_chat",
                repository=case.operation_repository,
            )
            crashed_context = first_gateway.operation("send-email")
            first_handle = await crashed_context.__aenter__()
            assert first_handle.should_execute is True
            await call_external(first_handle.idempotency_key)

            restarted_resolver = OpenInvocationResolver(
                event_repository=PostgresRuntimeEventRepository(case.settings),
                task_repository=CapabilityTaskRepository(case.settings),
                operation_repository=CapabilityOperationRepository(case.settings),
            )
            recovered = await restarted_resolver.resolve(
                run_id=case.submission.run_id
            )

            assert recovered is not None
            assert recovered.invocation_id == case.invocation.invocation_id
            assert recovered.task is not None
            assert recovered.task.task_id == case.resolution.task.task_id
            assert len(recovered.operations) == 1
            assert recovered.operations[0].operation_id == first_handle.operation_id
            assert (
                recovered.operations[0].idempotency_key
                == first_handle.idempotency_key
            )
            assert recovered.operations[0].status == "pending"

            restarted_gateway = CapabilityOperationGateway(
                run_id=recovered.run_id,
                invocation_id=recovered.invocation_id,
                task_id=recovered.task.task_id,
                capability_id=recovered.capability_id,
                repository=CapabilityOperationRepository(case.settings),
            )
            retry_context = restarted_gateway.operation("send-email")
            retry_handle = await retry_context.__aenter__()
            assert retry_handle.should_execute is True
            assert retry_handle.idempotency_key == first_handle.idempotency_key
            await call_external(retry_handle.idempotency_key)
            await retry_context.__aexit__(None, None, None)

            completed_context = restarted_gateway.operation("send-email")
            completed_handle = await completed_context.__aenter__()
            assert completed_handle.should_execute is False
            assert completed_handle.idempotency_key == first_handle.idempotency_key
            await completed_context.__aexit__(None, None, None)
            assert external_attempts == 2
            assert external_effects == {first_handle.idempotency_key}
        finally:
            await _cleanup_case(case)

    run_on_psycopg_compatible_loop(exercise())


def test_recovery_task_query_rejects_multiple_tasks_for_one_invocation() -> None:
    """同一 Invocation 的多条 Task 投影必须作为持久化冲突拒绝。"""

    from agent_runtime.capabilities.persistence import (
        CapabilityPersistenceConflictError,
        CapabilityTaskRepository,
    )
    from agent_runtime.capabilities.persistence_models import CapabilityTask

    async def exercise() -> None:
        case = await _prepare_case("duplicate-task")
        tasks = CapabilityTaskRepository(case.settings)
        now = datetime.now(UTC)
        duplicate = CapabilityTask(
            task_id=uuid4(),
            session_id=case.session_id,
            capability_id=case.resolution.task.capability_id,
            state_schema_version=case.resolution.task.state_schema_version,
            status="active",
            last_run_id=case.submission.run_id,
            last_invocation_id=case.invocation.invocation_id,
            created_at=now,
            updated_at=now,
            ended_at=None,
        )
        try:
            await tasks.add(duplicate)

            with pytest.raises(
                CapabilityPersistenceConflictError,
                match="同一 Invocation 关联了多个 Capability Task",
            ) as captured:
                await tasks.get_by_invocation(
                    run_id=case.submission.run_id,
                    invocation_id=case.invocation.invocation_id,
                )

            assert captured.value.code == "CAPABILITY_TASK_INVOCATION_CONFLICT"
        finally:
            await _cleanup_case(case)

    run_on_psycopg_compatible_loop(exercise())


def test_recovery_task_query_rejects_task_from_another_session() -> None:
    """Task 与 Run 不属于同一 Session 时不得用于恢复调用。"""

    from agent_runtime.capabilities.persistence import (
        CapabilityPersistenceConflictError,
        CapabilityTaskRepository,
    )
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    async def exercise() -> None:
        case = await _prepare_case("cross-session-task")
        tasks = CapabilityTaskRepository(case.settings)
        sessions = PostgresSessionRepository(case.settings)
        foreign_session_id = uuid4()
        now = datetime.now(UTC)
        await sessions.add(
            Session(
                session_id=foreign_session_id,
                user_id=case.settings.local_user_id,
                title="错误归属会话",
                created_at=now,
                updated_at=now,
            )
        )
        corrupted = False
        try:
            async with open_database_connection(case.settings) as connection:
                await connection.execute(
                    "DELETE FROM capability_task_contexts WHERE session_id = %s",
                    (case.session_id,),
                )
                await connection.execute(
                    "UPDATE capability_tasks SET session_id = %s WHERE task_id = %s",
                    (foreign_session_id, case.resolution.task.task_id),
                )
                await connection.commit()
                corrupted = True

            with pytest.raises(
                CapabilityPersistenceConflictError,
                match="Capability Task 与 Run 的 Session 不一致",
            ) as captured:
                await tasks.get_by_invocation(
                    run_id=case.submission.run_id,
                    invocation_id=case.invocation.invocation_id,
                )

            assert captured.value.code == "CAPABILITY_TASK_SESSION_MISMATCH"
        finally:
            if corrupted:
                async with open_database_connection(case.settings) as connection:
                    await connection.execute(
                        "DELETE FROM capability_tasks WHERE task_id = %s",
                        (case.resolution.task.task_id,),
                    )
                    await connection.execute(
                        "DELETE FROM sessions WHERE session_id = %s",
                        (foreign_session_id,),
                    )
                    await connection.commit()
            await _cleanup_case(case)

    run_on_psycopg_compatible_loop(exercise())


@pytest.mark.parametrize(
    ("checkpoint_first", "task_terminal_before_recovery"),
    [(False, False), (True, False), (False, True)],
)
def test_manual_recovery_with_postgres_checkpoint_fails_without_reexecution(
    checkpoint_first: bool,
    task_terminal_before_recovery: bool,
) -> None:
    """manual Capability 崩溃后只写 incomplete 与 failed，保留 Task/Operation。"""

    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, START, StateGraph

    from agent_runtime.capabilities.operation_ledger import (
        CapabilityOperationGateway,
    )
    from agent_runtime.capabilities.persistence import (
        CapabilityOperationRepository,
        CapabilityTaskRepository,
    )
    from agent_runtime.capabilities.recovery import (
        CapabilityRecoveryPolicyEvaluator,
        OpenInvocationResolver,
    )
    from agent_runtime.chat import ChatService
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.state import ParentState
    from agent_runtime.runtime.coordinator import RunCoordinator
    from agent_runtime.runtime.checkpoint_recovery import RunCheckpointInspector
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )
    from agent_runtime.runtime.event_schemas import (
        MessageStartedPayload,
        RunStartedPayload,
    )
    from agent_runtime.runtime.interrupts import (
        PostgresRunInterruptRepository,
    )
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.runtime.sequencer import RunSequencer
    from agent_runtime.sessions.repository import PostgresSessionRepository
    from agent_runtime.sessions.service import SessionService

    finish_calls = 0

    def prepare_node(_state):
        return {
            "resolved_capability_id": "general_chat",
            "completion_status": None,
        }

    def finish_node(_state):
        nonlocal finish_calls
        finish_calls += 1
        raise AssertionError("manual 恢复不得重新执行 Capability")

    def build_graph(checkpointer):
        builder = StateGraph(ParentState)
        builder.add_node("prepare", prepare_node)
        builder.add_node("invoke_capability", finish_node)
        builder.add_edge(START, "prepare")
        builder.add_edge("prepare", "invoke_capability")
        builder.add_edge("invoke_capability", END)
        return builder.compile(
            checkpointer=checkpointer,
            interrupt_before=["invoke_capability"],
        )

    def draft(event_type, payload):
        return RuntimeEventDraft(
            event_type=event_type,
            source="runtime.executor",
            visibility="public",
            payload=payload,
            schema_version=1,
            durability="durable",
            created_at=datetime.now(UTC),
        )

    async def exercise() -> None:
        manifest = _recovery_manifest(
            recovery_policy="manual",
            side_effect_policy="idempotent",
        )
        case = await _prepare_case("manual", manifest=manifest)
        runs = PostgresRunRepository(case.settings)
        events = PostgresRuntimeEventRepository(case.settings)
        tasks = CapabilityTaskRepository(case.settings)
        operations = CapabilityOperationRepository(case.settings)
        sessions = PostgresSessionRepository(case.settings)
        interrupts = PostgresRunInterruptRepository(case.settings)
        await interrupts.setup()
        sequencer = RunSequencer.for_run(
            run_id=case.submission.run_id,
            response_message_id=case.submission.response_message_id,
            repository=events,
        )
        await sequencer.transition_run(
            target_status="running",
            event=draft("run.started", RunStartedPayload(status="running")),
            updated_at=datetime.now(UTC),
        )
        await sequencer.emit(
            draft(
                "message.started",
                MessageStartedPayload(
                    response_message_id=case.submission.response_message_id,
                    attempt=1,
                ),
            )
        )
        gateway = CapabilityOperationGateway(
            run_id=case.submission.run_id,
            invocation_id=case.invocation.invocation_id,
            task_id=case.resolution.task.task_id,
            capability_id="general_chat",
            repository=operations,
        )
        pending_context = gateway.operation("send-email")
        pending_handle = await pending_context.__aenter__()
        expected_task = case.resolution.task

        try:
            async with AsyncPostgresSaver.from_conn_string(
                case.settings.database_connection_string
            ) as first_saver:
                await first_saver.setup()
                first_graph = build_graph(first_saver)
                config = parent_thread_config(
                    case.session_id,
                    message_id=case.submission.response_message_id,
                    run_id=case.submission.run_id,
                    request_id=case.submission.request_id,
                    input_message_id=case.submission.input_message_id,
                    response_message_id=case.submission.response_message_id,
                )
                async for _event in first_graph.astream(
                    {
                        "messages": [
                            HumanMessage(
                                content="manual 恢复测试",
                                id=str(case.submission.input_message_id),
                            )
                        ]
                    },
                    config,
                    stream_mode="custom",
                ):
                    pass
                intermediate = await first_graph.aget_state(config)
                assert intermediate.next == ("invoke_capability",)
                if checkpoint_first:
                    current = await runs.get(case.submission.run_id)
                    inspector = RunCheckpointInspector(parent_graph=first_graph)
                    snapshot = await inspector.latest_for_run(current)
                    assert snapshot is not None
                    precrash_service = ChatService(
                        settings=case.settings,
                        session_repository=sessions,
                        session_service=SessionService(
                            settings=case.settings,
                            session_repository=sessions,
                        ),
                        parent_graph=first_graph,
                    )
                    turn = await precrash_service._turn_from_run(
                        current,
                        recovery_config=inspector.recovery_config(
                            current,
                            snapshot,
                        ),
                    )
                    await precrash_service.persist_runtime_message(
                        turn,
                        "该能力需要人工恢复，当前回复未完成。",
                        runtime_status="incomplete",
                        capability_id="general_chat",
                    )

            if task_terminal_before_recovery:
                expected_task = await tasks.finish_and_clear_current(
                    task_id=case.resolution.task.task_id,
                    status="completed",
                    last_run_id=case.submission.run_id,
                    last_invocation_id=case.invocation.invocation_id,
                    updated_at=datetime.now(UTC),
                    ended_at=datetime.now(UTC),
                )

            resolver = OpenInvocationResolver(
                event_repository=events,
                task_repository=tasks,
                operation_repository=operations,
            )
            evaluator = CapabilityRecoveryPolicyEvaluator(
                registry=_Registry(manifest),
                invocation_resolver=resolver,
            )
            async with AsyncPostgresSaver.from_conn_string(
                case.settings.database_connection_string
            ) as restarted_saver:
                restarted_graph = build_graph(restarted_saver)
                coordinator = RunCoordinator(
                    repository=runs,
                    user_id=case.settings.local_user_id,
                    scan_interval_seconds=60,
                )
                service = ChatService(
                    settings=case.settings,
                    session_repository=sessions,
                    session_service=SessionService(
                        settings=case.settings,
                        session_repository=sessions,
                    ),
                    parent_graph=restarted_graph,
                    persistent_run_repository=runs,
                    runtime_event_repository=events,
                    run_interrupt_repository=interrupts,
                    run_coordinator=coordinator,
                    capability_recovery_policy_evaluator=evaluator,
                )
                try:
                    await coordinator.start(service.execute_persistent_run)
                    await coordinator.wait_until_idle()
                    failed = await runs.get(case.submission.run_id)
                    public_events = await events.list_public(
                        run_id=case.submission.run_id,
                        after_seq=0,
                        limit=100,
                    )
                    snapshot = await restarted_graph.aget_state(
                        parent_thread_config(case.session_id)
                    )
                    messages = [
                        message
                        for message in snapshot.values["messages"]
                        if isinstance(message, AIMessage)
                        and str(message.id)
                        == str(case.submission.response_message_id)
                    ]
                    preserved_task = await tasks.get(case.resolution.task.task_id)
                    preserved_operation = await operations.get_by_idempotency_key(
                        pending_handle.idempotency_key
                    )
                    open_invocation = await events.get_open_capability_invocation(
                        run_id=case.submission.run_id
                    )

                    assert finish_calls == 0
                    assert failed.status == "failed"
                    assert failed.error_code == (
                        "CAPABILITY_MANUAL_RECOVERY_REQUIRED"
                    )
                    assert len(messages) == 1
                    assert str(messages[0].text).strip()
                    assert messages[0].additional_kwargs["runtime_status"] == (
                        "incomplete"
                    )
                    assert [
                        event.event_type
                        for event in public_events
                        if event.event_type
                        in {"run.completed", "run.cancelled", "run.failed"}
                    ] == ["run.failed"]
                    assert preserved_task == expected_task
                    assert preserved_operation is not None
                    assert preserved_operation.status == "pending"
                    assert open_invocation is None
                finally:
                    await coordinator.close()
                    await restarted_saver.adelete_thread(str(case.session_id))
        finally:
            await _cleanup_case(case)

    run_on_psycopg_compatible_loop(exercise())


@pytest.mark.parametrize("task_terminal_before_recovery", [False, True])
def test_automatic_none_resumes_exact_postgres_checkpoint_for_dynamic_capability(
    task_terminal_before_recovery: bool,
) -> None:
    """automatic + none 应从精确 Checkpoint 恢复且只调用一次 Capability。"""

    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.config import get_stream_writer
    from langgraph.graph import END, START, StateGraph

    from agent_runtime.capabilities.persistence import (
        CapabilityOperationRepository,
        CapabilityTaskRepository,
    )
    from agent_runtime.capabilities.recovery import (
        CapabilityRecoveryPolicyEvaluator,
        OpenInvocationResolver,
    )
    from agent_runtime.chat import ChatService
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.state import ParentState
    from agent_runtime.runtime.coordinator import RunCoordinator
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )
    from agent_runtime.runtime.event_schemas import (
        MessageStartedPayload,
        RunStartedPayload,
    )
    from agent_runtime.runtime.interrupts import (
        PostgresRunInterruptRepository,
    )
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.runtime.sequencer import RunSequencer
    from agent_runtime.sessions.repository import PostgresSessionRepository
    from agent_runtime.sessions.service import SessionService

    capability_calls = 0
    capability_id = "dynamic_agent"

    def prepare_node(_state):
        return {
            "resolved_capability_id": capability_id,
            "completion_status": None,
        }

    def invoke_capability(_state, config):
        nonlocal capability_calls
        capability_calls += 1
        response = AIMessage(
            content="动态能力恢复完成。",
            id=config["configurable"]["message_id"],
            additional_kwargs={
                "runtime_status": "completed",
                "capability_id": capability_id,
            },
        )
        get_stream_writer()(response)
        return {
            "messages": [response],
            "completion_status": "completed",
        }

    def build_graph(checkpointer):
        builder = StateGraph(ParentState)
        builder.add_node("prepare", prepare_node)
        builder.add_node("invoke_capability", invoke_capability)
        builder.add_edge(START, "prepare")
        builder.add_edge("prepare", "invoke_capability")
        builder.add_edge("invoke_capability", END)
        return builder.compile(
            checkpointer=checkpointer,
            interrupt_before=["invoke_capability"],
        )

    def draft(event_type, payload):
        return RuntimeEventDraft(
            event_type=event_type,
            source="runtime.executor",
            visibility="public",
            payload=payload,
            schema_version=1,
            durability="durable",
            created_at=datetime.now(UTC),
        )

    async def exercise() -> None:
        manifest = _recovery_manifest(
            capability_id=capability_id,
            recovery_policy="automatic",
            side_effect_policy="none",
        )
        case = await _prepare_case("automatic-none", manifest=manifest)
        runs = PostgresRunRepository(case.settings)
        events = PostgresRuntimeEventRepository(case.settings)
        tasks = CapabilityTaskRepository(case.settings)
        operations = CapabilityOperationRepository(case.settings)
        sessions = PostgresSessionRepository(case.settings)
        interrupts = PostgresRunInterruptRepository(case.settings)
        await interrupts.setup()
        sequencer = RunSequencer.for_run(
            run_id=case.submission.run_id,
            response_message_id=case.submission.response_message_id,
            repository=events,
        )
        await sequencer.transition_run(
            target_status="running",
            event=draft("run.started", RunStartedPayload(status="running")),
            updated_at=datetime.now(UTC),
        )
        await sequencer.emit(
            draft(
                "message.started",
                MessageStartedPayload(
                    response_message_id=case.submission.response_message_id,
                    attempt=1,
                ),
            )
        )
        expected_task = case.resolution.task

        try:
            async with AsyncPostgresSaver.from_conn_string(
                case.settings.database_connection_string
            ) as first_saver:
                await first_saver.setup()
                first_graph = build_graph(first_saver)
                config = parent_thread_config(
                    case.session_id,
                    message_id=case.submission.response_message_id,
                    run_id=case.submission.run_id,
                    request_id=case.submission.request_id,
                    input_message_id=case.submission.input_message_id,
                    response_message_id=case.submission.response_message_id,
                )
                async for _event in first_graph.astream(
                    {
                        "messages": [
                            HumanMessage(
                                content="动态能力恢复测试",
                                id=str(case.submission.input_message_id),
                            )
                        ]
                    },
                    config,
                    stream_mode="custom",
                ):
                    pass
                intermediate = await first_graph.aget_state(config)
                assert intermediate.next == ("invoke_capability",)
                assert capability_calls == 0

            if task_terminal_before_recovery:
                expected_task = await tasks.finish_and_clear_current(
                    task_id=case.resolution.task.task_id,
                    status="completed",
                    last_run_id=case.submission.run_id,
                    last_invocation_id=case.invocation.invocation_id,
                    updated_at=datetime.now(UTC),
                    ended_at=datetime.now(UTC),
                )

            evaluator = CapabilityRecoveryPolicyEvaluator(
                registry=_Registry(manifest),
                invocation_resolver=OpenInvocationResolver(
                    event_repository=events,
                    task_repository=tasks,
                    operation_repository=operations,
                ),
            )
            async with AsyncPostgresSaver.from_conn_string(
                case.settings.database_connection_string
            ) as restarted_saver:
                restarted_graph = build_graph(restarted_saver)
                coordinator = RunCoordinator(
                    repository=runs,
                    user_id=case.settings.local_user_id,
                    scan_interval_seconds=60,
                )
                service = ChatService(
                    settings=case.settings,
                    session_repository=sessions,
                    session_service=SessionService(
                        settings=case.settings,
                        session_repository=sessions,
                    ),
                    parent_graph=restarted_graph,
                    persistent_run_repository=runs,
                    runtime_event_repository=events,
                    run_interrupt_repository=interrupts,
                    run_coordinator=coordinator,
                    capability_recovery_policy_evaluator=evaluator,
                )
                try:
                    await coordinator.start(service.execute_persistent_run)
                    await coordinator.wait_until_idle()
                    completed = await runs.get(case.submission.run_id)
                    public_events = await events.list_public(
                        run_id=case.submission.run_id,
                        after_seq=0,
                        limit=100,
                    )
                    attempts = [
                        event.payload["attempt"]
                        for event in public_events
                        if event.event_type == "message.started"
                    ]
                    snapshot = await restarted_graph.aget_state(
                        parent_thread_config(case.session_id)
                    )
                    replies = [
                        message
                        for message in snapshot.values["messages"]
                        if isinstance(message, AIMessage)
                        and str(message.id)
                        == str(case.submission.response_message_id)
                    ]
                    preserved_task = await tasks.get(case.resolution.task.task_id)
                    invocation_operations = await operations.list_for_invocation(
                        run_id=case.submission.run_id,
                        invocation_id=case.invocation.invocation_id,
                    )

                    assert completed.status == "completed"
                    assert completed.recovery_attempts == 1
                    assert capability_calls == 1
                    assert attempts == [1, 2]
                    assert len(replies) == 1
                    assert replies[0].additional_kwargs["capability_id"] == (
                        capability_id
                    )
                    assert preserved_task == expected_task
                    assert invocation_operations == ()
                finally:
                    await coordinator.close()
                    await restarted_saver.adelete_thread(str(case.session_id))
        finally:
            await _cleanup_case(case)

    run_on_psycopg_compatible_loop(exercise())


@pytest.mark.parametrize(
    (
        "crash_window",
        "expected_external_attempts",
        "expected_before_status",
        "expected_capability_calls",
        "expected_message_attempts",
    ),
    [
        ("before_operation", 1, None, 2, [1, 2]),
        ("after_external", 2, "pending", 2, [1, 2]),
        ("after_ledger", 1, "succeeded", 2, [1, 2]),
        ("after_task_transition", 1, "succeeded", 2, [1, 2]),
        ("after_parent_checkpoint", 1, "succeeded", 1, [1]),
    ],
)
def test_automatic_idempotent_reuses_operation_across_crash_windows(
    crash_window: str,
    expected_external_attempts: int,
    expected_before_status: str | None,
    expected_capability_calls: int,
    expected_message_attempts: list[int],
) -> None:
    """副作用与 Parent 落盘崩溃窗口都必须复用 Invocation 身份。"""

    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.config import get_stream_writer
    from langgraph.graph import END, START, StateGraph

    from agent_runtime.capabilities.operation_ledger import (
        CapabilityOperationGateway,
    )
    from agent_runtime.capabilities.invocation_service import (
        CapabilityInvocationService,
    )
    from agent_runtime.capabilities.persistence import (
        CapabilityOperationRepository,
        CapabilityPersistenceConflictError,
        CapabilityTaskRepository,
    )
    from agent_runtime.capabilities.recovery import (
        CapabilityRecoveryPolicyEvaluator,
        OpenInvocationResolver,
    )
    from agent_runtime.capabilities.task_service import CapabilityTaskService
    from agent_runtime.chat import ChatService
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.state import ParentState
    from agent_runtime.runtime.coordinator import RunCoordinator
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )
    from agent_runtime.runtime.event_schemas import (
        MessageStartedPayload,
        RunStartedPayload,
    )
    from agent_runtime.runtime.interrupts import (
        PostgresRunInterruptRepository,
    )
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.runtime.sequencer import RunSequencer
    from agent_runtime.sessions.repository import PostgresSessionRepository
    from agent_runtime.sessions.service import SessionService

    class SimulatedProcessCrash(RuntimeError):
        """只用于切断首次 Graph 执行且不形成 Runtime 失败投影。"""

    capability_id = "side_effect_agent"
    capability_calls = 0
    crash_pending = True
    external_attempts = 0
    external_effects: dict[str, str] = {}

    async def call_external(idempotency_key: str) -> str:
        nonlocal external_attempts
        external_attempts += 1
        return external_effects.setdefault(idempotency_key, "邮件已发送")

    def prepare_node(_state):
        return {
            "resolved_capability_id": capability_id,
            "completion_status": None,
        }

    async def exercise() -> None:
        nonlocal capability_calls, crash_pending
        manifest = _recovery_manifest(
            capability_id=capability_id,
            recovery_policy="automatic",
            side_effect_policy="idempotent",
        )
        case = await _prepare_case(
            f"idempotent-{crash_window}",
            manifest=manifest,
        )
        runs = PostgresRunRepository(case.settings)
        events = PostgresRuntimeEventRepository(case.settings)
        tasks = CapabilityTaskRepository(case.settings)
        operations = CapabilityOperationRepository(case.settings)
        sessions = PostgresSessionRepository(case.settings)
        interrupts = PostgresRunInterruptRepository(case.settings)
        await interrupts.setup()
        async def invoke_capability(_state, config):
            nonlocal capability_calls, crash_pending
            capability_calls += 1
            invocation_service = CapabilityInvocationService(
                sequencer=sequencer
            )
            invocation = await invocation_service.start(
                capability_id=capability_id,
                task_action="new",
                state_scope="session",
                created_at=datetime.now(UTC),
            )
            task_service = CapabilityTaskService(
                repository=tasks,
                sequencer=sequencer,
                invocation_service=invocation_service,
            )
            resolution = await task_service.resolve(
                manifest=manifest,
                session_id=case.session_id,
                run_id=case.submission.run_id,
                invocation_id=invocation.invocation_id,
                task_action=invocation.task_action,
                updated_at=datetime.now(UTC),
            )
            if crash_pending and crash_window == "before_operation":
                crash_pending = False
                raise SimulatedProcessCrash("副作用开始前崩溃")
            gateway = CapabilityOperationGateway(
                run_id=case.submission.run_id,
                invocation_id=invocation.invocation_id,
                task_id=resolution.task.task_id,
                capability_id=capability_id,
                repository=operations,
            )
            async with gateway.operation("send-email") as handle:
                if handle.should_execute:
                    await call_external(handle.idempotency_key)
                if crash_pending and crash_window == "after_external":
                    crash_pending = False
                    raise SimulatedProcessCrash("外部成功但 Ledger 未完成")
                result = external_effects.get(handle.idempotency_key)
            if crash_pending and crash_window == "after_ledger":
                crash_pending = False
                raise SimulatedProcessCrash("Ledger 成功但 Parent 未完成")
            await task_service.apply_transition(
                task=resolution.task,
                task_transition="completed",
                run_id=case.submission.run_id,
                invocation_id=invocation.invocation_id,
                updated_at=datetime.now(UTC),
            )
            if crash_pending and crash_window == "after_task_transition":
                with pytest.raises(
                    CapabilityPersistenceConflictError,
                    match="终态 Capability Task 不允许创建新的 Operation",
                ):
                    await gateway.operation("late-operation").__aenter__()
                crash_pending = False
                raise SimulatedProcessCrash("Task 终态已提交但 Invocation 未结束")
            response = AIMessage(
                content=result or "已从外部系统恢复结果。",
                id=config["configurable"]["message_id"],
                additional_kwargs={
                    "runtime_status": "completed",
                    "capability_id": capability_id,
                },
            )
            get_stream_writer()(response)
            return {
                "messages": [response],
                "completion_status": "completed",
            }

        def build_graph(checkpointer):
            builder = StateGraph(ParentState)
            builder.add_node("prepare", prepare_node)
            builder.add_node("invoke_capability", invoke_capability)
            builder.add_edge(START, "prepare")
            builder.add_edge("prepare", "invoke_capability")
            builder.add_edge("invoke_capability", END)
            return builder.compile(checkpointer=checkpointer)

        def draft(event_type, payload):
            return RuntimeEventDraft(
                event_type=event_type,
                source="runtime.executor",
                visibility="public",
                payload=payload,
                schema_version=1,
                durability="durable",
                created_at=datetime.now(UTC),
            )

        sequencer = RunSequencer.for_run(
            run_id=case.submission.run_id,
            response_message_id=case.submission.response_message_id,
            repository=events,
        )
        await sequencer.transition_run(
            target_status="running",
            event=draft("run.started", RunStartedPayload(status="running")),
            updated_at=datetime.now(UTC),
        )
        await sequencer.emit(
            draft(
                "message.started",
                MessageStartedPayload(
                    response_message_id=case.submission.response_message_id,
                    attempt=1,
                ),
            )
        )

        try:
            async with AsyncPostgresSaver.from_conn_string(
                case.settings.database_connection_string
            ) as first_saver:
                await first_saver.setup()
                first_graph = build_graph(first_saver)
                config = parent_thread_config(
                    case.session_id,
                    message_id=case.submission.response_message_id,
                    run_id=case.submission.run_id,
                    request_id=case.submission.request_id,
                    input_message_id=case.submission.input_message_id,
                    response_message_id=case.submission.response_message_id,
                )
                graph_stream = first_graph.astream(
                    {
                        "messages": [
                            HumanMessage(
                                content="幂等副作用恢复测试",
                                id=str(case.submission.input_message_id),
                            )
                        ]
                    },
                    config,
                    stream_mode="custom",
                )
                if crash_window == "after_parent_checkpoint":
                    async for _event in graph_stream:
                        pass
                else:
                    with pytest.raises(SimulatedProcessCrash):
                        async for _event in graph_stream:
                            pass

            resolver = OpenInvocationResolver(
                event_repository=events,
                task_repository=tasks,
                operation_repository=operations,
            )
            before_recovery = await resolver.resolve(
                run_id=case.submission.run_id
            )
            assert before_recovery is not None
            assert before_recovery.invocation_id == case.invocation.invocation_id
            assert before_recovery.task is not None
            assert before_recovery.task.task_id == case.resolution.task.task_id
            if expected_before_status is None:
                assert before_recovery.operations == ()
            else:
                assert len(before_recovery.operations) == 1
                assert before_recovery.operations[0].status == expected_before_status

            evaluator = CapabilityRecoveryPolicyEvaluator(
                registry=_Registry(manifest),
                invocation_resolver=resolver,
            )
            async with AsyncPostgresSaver.from_conn_string(
                case.settings.database_connection_string
            ) as restarted_saver:
                restarted_graph = build_graph(restarted_saver)
                coordinator = RunCoordinator(
                    repository=runs,
                    user_id=case.settings.local_user_id,
                    scan_interval_seconds=60,
                )
                service = ChatService(
                    settings=case.settings,
                    session_repository=sessions,
                    session_service=SessionService(
                        settings=case.settings,
                        session_repository=sessions,
                    ),
                    parent_graph=restarted_graph,
                    persistent_run_repository=runs,
                    runtime_event_repository=events,
                    run_interrupt_repository=interrupts,
                    run_coordinator=coordinator,
                    capability_recovery_policy_evaluator=evaluator,
                    open_invocation_resolver=resolver,
                )
                try:
                    await coordinator.start(service.execute_persistent_run)
                    await coordinator.wait_until_idle()
                    completed = await runs.get(case.submission.run_id)
                    recovered_open_invocation = await resolver.resolve(
                        run_id=case.submission.run_id
                    )
                    recovered_operations = await operations.list_for_invocation(
                        run_id=case.submission.run_id,
                        invocation_id=case.invocation.invocation_id,
                    )
                    public_events = await events.list_public(
                        run_id=case.submission.run_id,
                        after_seq=0,
                        limit=100,
                    )
                    snapshot = await restarted_graph.aget_state(
                        parent_thread_config(case.session_id)
                    )
                    replies = [
                        message
                        for message in snapshot.values["messages"]
                        if isinstance(message, AIMessage)
                        and str(message.id)
                        == str(case.submission.response_message_id)
                    ]

                    assert completed.status == "completed"
                    assert completed.recovery_attempts == 1
                    assert capability_calls == expected_capability_calls
                    assert external_attempts == expected_external_attempts
                    assert len(external_effects) == 1
                    assert recovered_open_invocation is None
                    assert len(recovered_operations) == 1
                    assert recovered_operations[0].status == "succeeded"
                    assert [
                        event.payload["attempt"]
                        for event in public_events
                        if event.event_type == "message.started"
                    ] == expected_message_attempts
                    assert len(replies) == 1
                    assert str(replies[0].text).strip()
                finally:
                    await coordinator.close()
                    await restarted_saver.adelete_thread(str(case.session_id))
        finally:
            await _cleanup_case(case)

    run_on_psycopg_compatible_loop(exercise())
