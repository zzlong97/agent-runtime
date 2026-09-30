"""S2.5-08 真实 LangGraph Checkpoint 与 PostgreSQL 恢复对账测试。"""

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_TESTS") != "1",
        reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
    ),
]


def _run_on_compatible_loop(coroutine):
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coroutine)


async def _create_running_run(*, settings, session_id, title):
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )
    from agent_runtime.runtime.event_schemas import (
        MessageStartedPayload,
        RunStartedPayload,
    )
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.models import RunSubmission
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.runtime.sequencer import RunSequencer
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository

    now = datetime.now(UTC)
    sessions = PostgresSessionRepository(settings)
    runs = PostgresRunRepository(settings)
    events = PostgresRuntimeEventRepository(settings)
    await sessions.setup()
    await runs.setup()
    await events.setup()
    await sessions.add(
        Session(
            session_id=session_id,
            user_id=settings.local_user_id,
            title=title,
            created_at=now,
            updated_at=now,
        )
    )
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
        input_payload={"message": {"content": title}},
        request_fingerprint=build_request_fingerprint(
            run_type="normal",
            session_id=session_id,
            request_payload={"message": {"content": title}},
        ),
        created_at=now,
    )
    await runs.create_or_get(submission)
    sequencer = RunSequencer.for_run(
        run_id=submission.run_id,
        response_message_id=submission.response_message_id,
        repository=events,
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

    await sequencer.transition_run(
        target_status="running",
        event=draft("run.started", RunStartedPayload(status="running")),
        updated_at=datetime.now(UTC),
    )
    await sequencer.emit(
        draft(
            "message.started",
            MessageStartedPayload(
                response_message_id=submission.response_message_id,
                attempt=1,
            ),
        )
    )
    return sessions, runs, events, submission


async def _delete_session_data(*, settings, session_id) -> None:
    from agent_runtime.persistence.database import open_database_connection

    async with open_database_connection(settings) as connection:
        await connection.execute(
            "DELETE FROM runs WHERE session_id = %s",
            (session_id,),
        )
        await connection.execute(
            "DELETE FROM sessions WHERE session_id = %s",
            (session_id,),
        )
        await connection.commit()


def test_final_checkpoint_is_projected_without_reexecuting_graph() -> None:
    """最终消息已落盘时，恢复器只补事件和 Run 终态。"""

    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.state import ParentState
    from agent_runtime.runtime.coordinator import RunCoordinator
    from agent_runtime.runtime.interrupts import (
        PostgresRunInterruptRepository,
    )
    from agent_runtime.sessions.service import SessionService

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-recovery-final-{uuid4()}",
        run_coordinator_scan_interval_seconds=60,
        _env_file=None,
    )
    session_id = uuid4()
    node_calls = 0

    def finish_node(_state, config):
        nonlocal node_calls
        node_calls += 1
        return {
            "messages": [
                AIMessage(
                    content="恢复前已经完成。",
                    id=config["configurable"]["message_id"],
                    additional_kwargs={
                        "runtime_status": "completed",
                        "capability_id": "general_chat",
                    },
                )
            ],
            "completion_status": "completed",
        }

    builder = StateGraph(ParentState)
    builder.add_node("finish", finish_node)
    builder.add_edge(START, "finish")
    builder.add_edge("finish", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    async def exercise() -> None:
        sessions, runs, events, submission = await _create_running_run(
            settings=settings,
            session_id=session_id,
            title="最终 Checkpoint 恢复",
        )
        interrupts = PostgresRunInterruptRepository(settings)
        await interrupts.setup()
        config = parent_thread_config(
            session_id,
            message_id=submission.response_message_id,
            run_id=submission.run_id,
            response_message_id=submission.response_message_id,
        )
        await graph.ainvoke(
            {
                "messages": [
                    HumanMessage(
                        content="最终 Checkpoint 恢复",
                        id=str(submission.input_message_id),
                    )
                ]
            },
            config,
        )
        assert node_calls == 1

        coordinator = RunCoordinator(
            repository=runs,
            user_id=settings.local_user_id,
            scan_interval_seconds=60,
        )
        service = ChatService(
            settings=settings,
            session_repository=sessions,
            session_service=SessionService(
                settings=settings,
                session_repository=sessions,
            ),
            parent_graph=graph,
            persistent_run_repository=runs,
            runtime_event_repository=events,
            run_interrupt_repository=interrupts,
            run_coordinator=coordinator,
        )
        executor_errors = []

        async def execute_with_evidence(run, dispatch_reason):
            try:
                await service.execute_persistent_run(run, dispatch_reason)
            except Exception as error:
                executor_errors.append(error)
                raise

        try:
            await coordinator.start(execute_with_evidence)
            await coordinator.wait_until_idle()
            recovered = await runs.get(submission.run_id)
            public_events = await events.list_public(
                run_id=submission.run_id,
                after_seq=0,
                limit=100,
            )
            if executor_errors:
                raise executor_errors[0]
            assert recovered.status == "completed"
            assert recovered.recovery_attempts == 1
            assert node_calls == 1
            assert [event.event_type for event in public_events] == [
                "run.started",
                "message.started",
                "message.finalized",
                "run.completed",
            ]
        finally:
            await coordinator.close()
            await _delete_session_data(
                settings=settings,
                session_id=session_id,
            )

    _run_on_compatible_loop(exercise())


def test_interrupt_checkpoint_is_projected_without_reexecuting_graph() -> None:
    """中断快照已落盘时，恢复器只补 pending Interrupt 与事件。"""

    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.state import ParentState
    from agent_runtime.runtime.coordinator import RunCoordinator
    from agent_runtime.runtime.interrupts import (
        PostgresRunInterruptRepository,
    )
    from agent_runtime.sessions.service import SessionService

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-recovery-interrupt-{uuid4()}",
        run_coordinator_scan_interval_seconds=60,
        _env_file=None,
    )
    session_id = uuid4()
    node_calls = 0

    def approval_node(_state):
        nonlocal node_calls
        node_calls += 1
        interrupt({"prompt": "是否继续恢复？"})
        raise AssertionError("未 Resume 时不应越过 Interrupt")

    builder = StateGraph(ParentState)
    builder.add_node("approval", approval_node)
    builder.add_edge(START, "approval")
    builder.add_edge("approval", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    async def exercise() -> None:
        sessions, runs, events, submission = await _create_running_run(
            settings=settings,
            session_id=session_id,
            title="Interrupt Checkpoint 恢复",
        )
        interrupts = PostgresRunInterruptRepository(settings)
        await interrupts.setup()
        config = parent_thread_config(
            session_id,
            message_id=submission.response_message_id,
            run_id=submission.run_id,
            response_message_id=submission.response_message_id,
        )
        await graph.ainvoke(
            {
                "messages": [
                    HumanMessage(
                        content="Interrupt Checkpoint 恢复",
                        id=str(submission.input_message_id),
                    )
                ]
            },
            config,
        )
        assert node_calls == 1

        coordinator = RunCoordinator(
            repository=runs,
            user_id=settings.local_user_id,
            scan_interval_seconds=60,
        )
        service = ChatService(
            settings=settings,
            session_repository=sessions,
            session_service=SessionService(
                settings=settings,
                session_repository=sessions,
            ),
            parent_graph=graph,
            persistent_run_repository=runs,
            runtime_event_repository=events,
            run_interrupt_repository=interrupts,
            run_coordinator=coordinator,
        )
        executor_errors = []

        async def execute_with_evidence(run, dispatch_reason):
            try:
                await service.execute_persistent_run(run, dispatch_reason)
            except Exception as error:
                executor_errors.append(error)
                raise

        try:
            await coordinator.start(execute_with_evidence)
            await coordinator.wait_until_idle()
            recovered = await runs.get(submission.run_id)
            pending = await interrupts.get_pending(run_id=submission.run_id)
            if executor_errors:
                raise executor_errors[0]
            assert recovered.status == "interrupted"
            assert recovered.recovery_attempts == 1
            assert pending is not None
            assert pending.interrupt_payload == {
                "prompt": "是否继续恢复？"
            }
            assert node_calls == 1
        finally:
            await coordinator.close()
            await _delete_session_data(
                settings=settings,
                session_id=session_id,
            )

    _run_on_compatible_loop(exercise())


def test_run_without_checkpoint_replays_input_with_new_message_attempt() -> None:
    """首 Checkpoint 前崩溃应重放输入，并以新 attempt 替换旧草稿。"""

    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.config import get_stream_writer
    from langgraph.graph import END, START, StateGraph

    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.state import ParentState
    from agent_runtime.runtime.coordinator import RunCoordinator
    from agent_runtime.runtime.interrupts import (
        PostgresRunInterruptRepository,
    )
    from agent_runtime.sessions.service import SessionService

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-recovery-replay-{uuid4()}",
        run_coordinator_scan_interval_seconds=60,
        _env_file=None,
    )
    session_id = uuid4()
    node_calls = 0

    def invoke_capability(_state, config):
        nonlocal node_calls
        node_calls += 1
        response = AIMessage(
            content="恢复重放完成。",
            id=config["configurable"]["message_id"],
            additional_kwargs={
                "runtime_status": "completed",
                "capability_id": "general_chat",
            },
        )
        get_stream_writer()(response)
        return {
            "messages": [response],
            "completion_status": "completed",
        }

    builder = StateGraph(ParentState)
    builder.add_node("invoke_capability", invoke_capability)
    builder.add_edge(START, "invoke_capability")
    builder.add_edge("invoke_capability", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    async def exercise() -> None:
        sessions, runs, events, submission = await _create_running_run(
            settings=settings,
            session_id=session_id,
            title="首 Checkpoint 前恢复",
        )
        interrupts = PostgresRunInterruptRepository(settings)
        await interrupts.setup()
        coordinator = RunCoordinator(
            repository=runs,
            user_id=settings.local_user_id,
            scan_interval_seconds=60,
        )
        service = ChatService(
            settings=settings,
            session_repository=sessions,
            session_service=SessionService(
                settings=settings,
                session_repository=sessions,
            ),
            parent_graph=graph,
            persistent_run_repository=runs,
            runtime_event_repository=events,
            run_interrupt_repository=interrupts,
            run_coordinator=coordinator,
        )
        try:
            await coordinator.start(service.execute_persistent_run)
            await coordinator.wait_until_idle()
            recovered = await runs.get(submission.run_id)
            public_events = await events.list_public(
                run_id=submission.run_id,
                after_seq=0,
                limit=100,
            )
            attempts = [
                event.payload["attempt"]
                for event in public_events
                if event.event_type == "message.started"
            ]
            snapshot = await graph.aget_state(
                parent_thread_config(session_id)
            )
            human_ids = [
                str(message.id)
                for message in snapshot.values["messages"]
                if isinstance(message, HumanMessage)
            ]
            assert recovered.status == "completed"
            assert recovered.recovery_attempts == 1
            assert attempts == [1, 2]
            assert node_calls == 1
            assert human_ids == [str(submission.input_message_id)]
        finally:
            await coordinator.close()
            await _delete_session_data(
                settings=settings,
                session_id=session_id,
            )

    _run_on_compatible_loop(exercise())


def test_third_failed_recovery_forms_one_nonempty_failed_terminal() -> None:
    """三次接管均未形成 Checkpoint 时，不再执行并形成唯一失败终态。"""

    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.state import ParentState
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.coordinator import RunCoordinator
    from agent_runtime.runtime.interrupts import (
        PostgresRunInterruptRepository,
    )
    from agent_runtime.sessions.service import SessionService

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-recovery-exhausted-{uuid4()}",
        run_coordinator_scan_interval_seconds=60,
        _env_file=None,
    )
    session_id = uuid4()
    node_calls = 0

    def invoke_capability(_state):
        nonlocal node_calls
        node_calls += 1
        raise AssertionError("恢复次数耗尽后不得再次执行 Graph")

    builder = StateGraph(ParentState)
    builder.add_node("invoke_capability", invoke_capability)
    builder.add_edge(START, "invoke_capability")
    builder.add_edge("invoke_capability", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    async def exercise() -> None:
        sessions, runs, events, submission = await _create_running_run(
            settings=settings,
            session_id=session_id,
            title="恢复次数耗尽",
        )
        async with open_database_connection(settings) as connection:
            await connection.execute(
                "UPDATE runs SET recovery_attempts = 3 WHERE run_id = %s",
                (submission.run_id,),
            )
            await connection.commit()
        interrupts = PostgresRunInterruptRepository(settings)
        await interrupts.setup()
        coordinator = RunCoordinator(
            repository=runs,
            user_id=settings.local_user_id,
            scan_interval_seconds=60,
        )
        service = ChatService(
            settings=settings,
            session_repository=sessions,
            session_service=SessionService(
                settings=settings,
                session_repository=sessions,
            ),
            parent_graph=graph,
            persistent_run_repository=runs,
            runtime_event_repository=events,
            run_interrupt_repository=interrupts,
            run_coordinator=coordinator,
        )
        try:
            await coordinator.start(service.execute_persistent_run)
            await coordinator.wait_until_idle()
            failed = await runs.get(submission.run_id)
            public_events = await events.list_public(
                run_id=submission.run_id,
                after_seq=0,
                limit=100,
            )
            terminal_events = [
                event
                for event in public_events
                if event.event_type
                in {"run.completed", "run.cancelled", "run.failed"}
            ]
            snapshot = await graph.aget_state(
                parent_thread_config(session_id)
            )
            failed_messages = [
                message
                for message in snapshot.values["messages"]
                if isinstance(message, AIMessage)
                and str(message.id) == str(submission.response_message_id)
            ]
            assert failed.status == "failed"
            assert failed.recovery_attempts == 3
            assert failed.error_code == "RUN_RECOVERY_EXHAUSTED"
            assert node_calls == 0
            assert len(terminal_events) == 1
            assert terminal_events[0].event_type == "run.failed"
            assert len(failed_messages) == 1
            assert str(failed_messages[0].text).strip()
            assert failed_messages[0].additional_kwargs["runtime_status"] == (
                "incomplete"
            )
        finally:
            await coordinator.close()
            await _delete_session_data(
                settings=settings,
                session_id=session_id,
            )

    _run_on_compatible_loop(exercise())


def test_intermediate_checkpoint_resumes_exactly_with_new_attempt() -> None:
    """流式执行中断后应从目标 Run 的中间 Checkpoint 续跑并递增 attempt。"""

    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.config import get_stream_writer
    from langgraph.graph import END, START, StateGraph

    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.state import ParentState
    from agent_runtime.runtime.coordinator import RunCoordinator
    from agent_runtime.runtime.interrupts import (
        PostgresRunInterruptRepository,
    )
    from agent_runtime.sessions.service import SessionService

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-recovery-middle-{uuid4()}",
        run_coordinator_scan_interval_seconds=60,
        _env_file=None,
    )
    session_id = uuid4()
    finish_calls = 0

    def prepare_node(_state):
        return {
            "resolved_capability_id": "general_chat",
            "completion_status": None,
        }

    def finish_node(_state, config):
        nonlocal finish_calls
        finish_calls += 1
        response = AIMessage(
            content="从中间 Checkpoint 恢复完成。",
            id=config["configurable"]["message_id"],
            additional_kwargs={
                "runtime_status": "completed",
                "capability_id": "general_chat",
            },
        )
        get_stream_writer()(response)
        return {
            "messages": [response],
            "completion_status": "completed",
        }

    builder = StateGraph(ParentState)
    builder.add_node("prepare", prepare_node)
    builder.add_node("finish", finish_node)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "finish")
    builder.add_edge("finish", END)
    graph = builder.compile(
        checkpointer=InMemorySaver(),
        interrupt_before=["finish"],
    )

    async def exercise() -> None:
        sessions, runs, events, submission = await _create_running_run(
            settings=settings,
            session_id=session_id,
            title="中间 Checkpoint 恢复",
        )
        interrupts = PostgresRunInterruptRepository(settings)
        await interrupts.setup()
        config = parent_thread_config(
            session_id,
            message_id=submission.response_message_id,
            run_id=submission.run_id,
            response_message_id=submission.response_message_id,
        )
        async for _event in graph.astream(
            {
                "messages": [
                    HumanMessage(
                        content="中间 Checkpoint 恢复",
                        id=str(submission.input_message_id),
                    )
                ]
            },
            config,
            stream_mode="custom",
        ):
            pass
        intermediate = await graph.aget_state(config)
        assert intermediate.next == ("finish",)
        assert not intermediate.interrupts
        assert finish_calls == 0

        coordinator = RunCoordinator(
            repository=runs,
            user_id=settings.local_user_id,
            scan_interval_seconds=60,
        )
        service = ChatService(
            settings=settings,
            session_repository=sessions,
            session_service=SessionService(
                settings=settings,
                session_repository=sessions,
            ),
            parent_graph=graph,
            persistent_run_repository=runs,
            runtime_event_repository=events,
            run_interrupt_repository=interrupts,
            run_coordinator=coordinator,
        )
        try:
            await coordinator.start(service.execute_persistent_run)
            await coordinator.wait_until_idle()
            recovered = await runs.get(submission.run_id)
            public_events = await events.list_public(
                run_id=submission.run_id,
                after_seq=0,
                limit=100,
            )
            attempts = [
                event.payload["attempt"]
                for event in public_events
                if event.event_type == "message.started"
            ]
            latest = await graph.aget_state(parent_thread_config(session_id))
            human_ids = [
                str(message.id)
                for message in latest.values["messages"]
                if isinstance(message, HumanMessage)
            ]
            assert recovered.status == "completed"
            assert recovered.recovery_attempts == 1
            assert attempts == [1, 2]
            assert finish_calls == 1
            assert human_ids == [str(submission.input_message_id)]
        finally:
            await coordinator.close()
            await _delete_session_data(
                settings=settings,
                session_id=session_id,
            )

    _run_on_compatible_loop(exercise())


@pytest.mark.parametrize(
    "crashed_status",
    ["queued", "running", "interrupted", "cancel_requested"],
)
def test_stopped_checkpoint_reconciles_cancel_without_graph_reexecution(
    crashed_status: str,
) -> None:
    """取消消息先落盘后重启时，只补唯一取消投影且不得重跑 Graph。"""

    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, START, StateGraph

    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.state import ParentState
    from agent_runtime.runtime.coordinator import RunCoordinator
    from agent_runtime.runtime.event_models import RuntimeEventDraft
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )
    from agent_runtime.runtime.event_schemas import (
        InterruptRequiredPayload,
        MessageFinalizedPayload,
        MessageStartedPayload,
        RunCancelRequestedPayload,
        RunStartedPayload,
    )
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.interrupts import (
        PostgresRunInterruptRepository,
    )
    from agent_runtime.runtime.models import RunSubmission
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.runtime.sequencer import RunSequencer
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository
    from agent_runtime.sessions.service import SessionService

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-stopped-recovery-{crashed_status}-{uuid4()}",
        run_coordinator_scan_interval_seconds=60,
        _env_file=None,
    )
    session_id = uuid4()
    now = datetime.now(UTC)
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
        input_payload={"message": {"content": "取消崩溃窗口测试"}},
        request_fingerprint=build_request_fingerprint(
            run_type="normal",
            session_id=session_id,
            request_payload={"message": {"content": "取消崩溃窗口测试"}},
        ),
        created_at=now,
    )
    graph_calls = 0

    def invoke_capability(_state):
        nonlocal graph_calls
        graph_calls += 1
        raise AssertionError("stopped Checkpoint 已存在时不得重新执行 Graph")

    def build_graph(checkpointer):
        builder = StateGraph(ParentState)
        builder.add_node("invoke_capability", invoke_capability)
        builder.add_edge(START, "invoke_capability")
        builder.add_edge("invoke_capability", END)
        return builder.compile(checkpointer=checkpointer)

    def draft(event_type, payload, *, visibility="public"):
        return RuntimeEventDraft(
            event_type=event_type,
            source="runtime.executor",
            visibility=visibility,
            payload=payload,
            schema_version=1,
            durability="durable",
            created_at=datetime.now(UTC),
        )

    async def exercise() -> None:
        sessions = PostgresSessionRepository(settings)
        runs = PostgresRunRepository(settings)
        events = PostgresRuntimeEventRepository(settings)
        interrupts = PostgresRunInterruptRepository(settings)
        await sessions.setup()
        await runs.setup()
        await events.setup()
        await interrupts.setup()
        await sessions.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="取消崩溃窗口测试",
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
        if crashed_status != "queued":
            await sequencer.transition_run(
                target_status="running",
                event=draft(
                    "run.started",
                    RunStartedPayload(status="running"),
                ),
                updated_at=datetime.now(UTC),
            )
            await sequencer.emit(
                draft(
                    "message.started",
                    MessageStartedPayload(
                        response_message_id=submission.response_message_id,
                        attempt=1,
                    ),
                )
            )
        if crashed_status == "interrupted":
            interrupt_id = uuid4()
            await sequencer.require_interrupt(
                interrupt_id=interrupt_id,
                interrupt_payload={"prompt": "是否停止？"},
                event=draft(
                    "interrupt.required",
                    InterruptRequiredPayload(interrupt_id=interrupt_id),
                ),
                updated_at=datetime.now(UTC),
            )
        elif crashed_status == "cancel_requested":
            await sequencer.transition_run(
                target_status="cancel_requested",
                event=draft(
                    "internal.run.cancel_requested",
                    RunCancelRequestedPayload(status="cancel_requested"),
                    visibility="internal",
                ),
                updated_at=datetime.now(UTC),
            )

        try:
            async with AsyncPostgresSaver.from_conn_string(
                settings.database_connection_string
            ) as first_saver:
                await first_saver.setup()
                first_graph = build_graph(first_saver)
                first_coordinator = RunCoordinator(
                    repository=runs,
                    user_id=settings.local_user_id,
                    scan_interval_seconds=60,
                )
                first_service = ChatService(
                    settings=settings,
                    session_repository=sessions,
                    session_service=SessionService(
                        settings=settings,
                        session_repository=sessions,
                    ),
                    parent_graph=first_graph,
                    persistent_run_repository=runs,
                    runtime_event_repository=events,
                    run_interrupt_repository=interrupts,
                    run_coordinator=first_coordinator,
                )
                current = await runs.get(submission.run_id)
                turn = await first_service._turn_from_run(current)
                await first_service.persist_runtime_message(
                    turn,
                    "已停止本次回复。",
                    runtime_status="stopped",
                    capability_id="general_chat",
                    include_human=True,
                )
                await first_coordinator.close()

            # 模拟进程在 message.finalized 已提交、run.cancelled 尚未提交时崩溃。
            await sequencer.emit(
                draft(
                    "message.finalized",
                    MessageFinalizedPayload(
                        response_message_id=submission.response_message_id,
                        runtime_status="stopped",
                        capability_id="general_chat",
                    ),
                )
            )
            del sequencer

            async with AsyncPostgresSaver.from_conn_string(
                settings.database_connection_string
            ) as restarted_saver:
                restarted_graph = build_graph(restarted_saver)
                restarted_coordinator = RunCoordinator(
                    repository=runs,
                    user_id=settings.local_user_id,
                    scan_interval_seconds=60,
                )
                restarted_service = ChatService(
                    settings=settings,
                    session_repository=sessions,
                    session_service=SessionService(
                        settings=settings,
                        session_repository=sessions,
                    ),
                    parent_graph=restarted_graph,
                    persistent_run_repository=runs,
                    runtime_event_repository=events,
                    run_interrupt_repository=interrupts,
                    run_coordinator=restarted_coordinator,
                )
                try:
                    await restarted_coordinator.start(
                        restarted_service.execute_persistent_run
                    )
                    await restarted_coordinator.wait_until_idle()
                    recovered = await runs.get(submission.run_id)
                    public_events = await events.list_public(
                        run_id=submission.run_id,
                        after_seq=0,
                        limit=100,
                    )
                    snapshot = await restarted_graph.aget_state(
                        parent_thread_config(session_id)
                    )
                    stopped_messages = [
                        message
                        for message in snapshot.values["messages"]
                        if isinstance(message, AIMessage)
                        and str(message.id)
                        == str(submission.response_message_id)
                        and message.additional_kwargs.get("runtime_status")
                        == "stopped"
                    ]
                    human_messages = [
                        message
                        for message in snapshot.values["messages"]
                        if isinstance(message, HumanMessage)
                        and str(message.id) == str(submission.input_message_id)
                    ]
                    event_types = [event.event_type for event in public_events]

                    assert recovered.status == "cancelled"
                    assert graph_calls == 0
                    assert event_types.count("message.finalized") == 1
                    assert event_types.count("run.cancelled") == 1
                    assert "run.failed" not in event_types
                    assert len(stopped_messages) == 1
                    assert str(stopped_messages[0].text).strip()
                    assert len(human_messages) == 1
                    if crashed_status == "interrupted":
                        assert (
                            await interrupts.get_pending(
                                run_id=submission.run_id
                            )
                            is None
                        )
                finally:
                    await restarted_coordinator.close()
                    await restarted_saver.adelete_thread(str(session_id))
        finally:
            await _delete_session_data(
                settings=settings,
                session_id=session_id,
            )

    _run_on_compatible_loop(exercise())
