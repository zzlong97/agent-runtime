"""S2.5-07 LangGraph Interrupt 与同 Run Resume 集成验证。"""

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import httpx


def test_regenerate_interrupt_resumes_latest_fork_checkpoint() -> None:
    """Regenerate 从已捕获起点 fork 后，Interrupt 与 Resume 应使用最新分支。"""

    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.config import get_stream_writer
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.state import ParentState
    from agent_runtime.runtime.models import Run

    session_id = uuid4()
    seed_response_id = uuid4()
    regenerate_response_id = uuid4()
    now = datetime(2026, 9, 29, 12, 30, tzinfo=UTC)

    def prepare_node(_state):
        return {"completion_status": None}

    def invoke_node(state, config):
        if config["configurable"]["message_id"] == str(seed_response_id):
            response = AIMessage(
                content="种子回答。",
                id=str(seed_response_id),
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
        answer = interrupt({"prompt": "是否重新生成？"})
        assert answer == {"approved": True}
        response = AIMessage(
            content="已重新生成。",
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
    builder.add_node("invoke_capability", invoke_node)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "invoke_capability")
    builder.add_edge("invoke_capability", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    async def exercise() -> None:
        seed_config = parent_thread_config(
            session_id,
            message_id=seed_response_id,
        )
        seed_human_id = uuid4()
        async for _event in graph.astream(
            {
                "messages": [
                    HumanMessage(
                        content="请给出种子回答",
                        id=str(seed_human_id),
                    )
                ]
            },
            seed_config,
            stream_mode="custom",
        ):
            pass
        start_snapshot = None
        async for snapshot in graph.aget_state_history(seed_config):
            if snapshot.next == ("invoke_capability",):
                start_snapshot = snapshot
                break
        assert start_snapshot is not None
        run = Run(
            run_id=uuid4(),
            request_id=uuid4(),
            session_id=session_id,
            thread_id=str(session_id),
            parent_run_id=None,
            run_type="regenerate",
            input_message_id=seed_human_id,
            response_message_id=regenerate_response_id,
            start_checkpoint_id=str(
                start_snapshot.config["configurable"]["checkpoint_id"]
            ),
            input_payload={
                "source_message_id": str(seed_response_id),
            },
            request_fingerprint="e" * 64,
            status="running",
            recovery_attempts=0,
            seq_high_watermark=0,
            error_code=None,
            error_message=None,
            created_at=now,
            started_at=now,
            finished_at=None,
            updated_at=now,
        )
        service = ChatService(
            settings=Settings(_env_file=None),
            session_repository=object(),
            session_service=object(),
            parent_graph=graph,
        )
        initial_turn = await service._turn_from_run(run)
        assert initial_turn.config["metadata"] == {
            "run_id": str(run.run_id),
            "request_id": str(run.request_id),
            "input_message_id": str(run.input_message_id),
            "response_message_id": str(regenerate_response_id),
        }
        assert [
            message async for message in service.stream_turn(initial_turn)
        ] == []
        pending = await service._pending_graph_interrupt(
            run=run,
            turn=initial_turn,
        )
        assert pending is not None
        assert pending[1] == {"prompt": "是否重新生成？"}

        resumed_turn = await service._turn_from_run(
            run,
            is_resume=True,
            resume_payload={"approved": True},
        )
        resumed_messages = [
            message async for message in service.stream_turn(resumed_turn)
        ]
        assert [message.id for message in resumed_messages] == [
            str(regenerate_response_id)
        ]
        latest = await graph.aget_state(parent_thread_config(session_id))
        assert [
            message.id
            for message in latest.values["messages"]
            if isinstance(message, HumanMessage)
        ] == [str(seed_human_id)]

    asyncio.run(exercise())


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="设置 RUN_POSTGRES_TESTS=1 后运行 PostgreSQL 集成测试",
)
def test_graph_interrupt_resumes_same_run_without_new_human_message() -> None:
    """Checkpoint 中断后应沿用 Run 和消息标识，并只保留一条 HumanMessage。"""

    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.config import get_stream_writer
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.state import ParentState
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.runtime.coordinator import RunCoordinator
    from agent_runtime.runtime.event_repository import (
        PostgresRuntimeEventRepository,
    )
    from agent_runtime.runtime.fingerprints import build_request_fingerprint
    from agent_runtime.runtime.interrupts import (
        PostgresRunInterruptRepository,
    )
    from agent_runtime.runtime.models import RunSubmission
    from agent_runtime.runtime.repository import PostgresRunRepository
    from agent_runtime.sessions.models import Session
    from agent_runtime.sessions.repository import PostgresSessionRepository
    from agent_runtime.sessions.service import SessionService
    from agent_runtime.main import create_app

    base_settings = Settings()
    settings = Settings(
        database_url=base_settings.database_url,
        local_user_id=f"s25-interrupt-graph-{uuid4()}",
        run_coordinator_scan_interval_seconds=60,
        _env_file=None,
    )
    session_id = uuid4()
    now = datetime(2026, 9, 29, 13, 0, tzinfo=UTC)
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
        input_payload={"message": {"content": "请执行需要确认的操作"}},
        request_fingerprint=build_request_fingerprint(
            run_type="normal",
            session_id=session_id,
            request_payload={
                "message": {"content": "请执行需要确认的操作"}
            },
        ),
        created_at=now,
    )

    def approval_node(state, config):
        """测试专用单中断节点，不进入正式 Capability 实现。"""

        latest_human = next(
            message
            for message in reversed(state["messages"])
            if isinstance(message, HumanMessage)
        )
        if latest_human.text == "已有会话种子消息":
            response = AIMessage(
                content="已有会话种子回复。",
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
        answer = interrupt({"prompt": "是否允许继续执行？"})
        assert answer == {"approved": True}
        response = AIMessage(
            content="已按确认继续执行。",
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
    builder.add_node("invoke_capability", approval_node)
    builder.add_edge(START, "invoke_capability")
    builder.add_edge("invoke_capability", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    async def exercise() -> None:
        nonlocal submission
        sessions = PostgresSessionRepository(settings)
        runs = PostgresRunRepository(settings)
        events = PostgresRuntimeEventRepository(settings)
        interrupts = PostgresRunInterruptRepository(settings)
        await sessions.setup()
        await runs.setup()
        await events.setup()
        await sessions.add(
            Session(
                session_id=session_id,
                user_id=settings.local_user_id,
                title="Interrupt Graph 验收",
                created_at=now,
                updated_at=now,
            )
        )
        seed_human_id = uuid4()
        seed_response_id = uuid4()
        seed_config = parent_thread_config(
            session_id,
            message_id=seed_response_id,
        )
        async for _event in graph.astream(
            {
                "messages": [
                    HumanMessage(
                        content="已有会话种子消息",
                        id=str(seed_human_id),
                    )
                ]
            },
            seed_config,
            stream_mode="custom",
        ):
            pass
        seed_snapshot = await graph.aget_state(seed_config)
        seed_checkpoint_id = seed_snapshot.config["configurable"][
            "checkpoint_id"
        ]
        assert seed_checkpoint_id
        submission = replace(
            submission,
            start_checkpoint_id=str(seed_checkpoint_id),
        )
        await runs.create_or_get(submission)
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

        await coordinator.start(
            execute_with_evidence,
            scan_on_startup=False,
        )
        try:
            await coordinator.wake(submission.run_id)
            await coordinator.wait_for_run(submission.run_id)

            interrupted_run = await runs.get(submission.run_id)
            pending = await interrupts.get_pending(run_id=submission.run_id)
            assert executor_errors == []
            assert interrupted_run.status == "interrupted"
            assert pending is not None
            assert pending.interrupt_payload == {
                "prompt": "是否允许继续执行？"
            }

            app = create_app(chat_service=service)
            transport = httpx.ASGITransport(app=app)
            resume_request_id = uuid4()
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                active = await client.get(
                    f"/api/v1/chat/sessions/{session_id}/active-run"
                )
                assert active.status_code == 200
                assert active.json()["pending_interrupt"] == {
                    "interrupt_id": str(pending.interrupt_id),
                    "interrupt_payload": {
                        "prompt": "是否允许继续执行？"
                    },
                    "created_at": pending.created_at.isoformat().replace(
                        "+00:00", "Z"
                    ),
                }
                resume_body = {
                    "interrupt_id": str(pending.interrupt_id),
                    "request_id": str(resume_request_id),
                    "resume_payload": {"approved": True},
                }
                resumed = await client.post(
                    f"/api/v1/chat/runs/{submission.run_id}/resume",
                    json=resume_body,
                )
                repeated = await client.post(
                    f"/api/v1/chat/runs/{submission.run_id}/resume",
                    json=resume_body,
                )
                conflicting = await client.post(
                    f"/api/v1/chat/runs/{submission.run_id}/resume",
                    json={
                        **resume_body,
                        "resume_payload": {"approved": False},
                    },
                )
            assert resumed.status_code == 202
            assert repeated.status_code == 202
            assert resumed.json()["run_id"] == str(submission.run_id)
            assert repeated.json()["run_id"] == str(submission.run_id)
            assert conflicting.status_code == 409
            assert conflicting.json()["detail"]["code"] == (
                "INTERRUPT_REQUEST_CONFLICT"
            )
            await coordinator.wait_for_run(submission.run_id)

            completed = await runs.get(submission.run_id)
            assert completed.run_id == submission.run_id
            assert completed.status == "completed"
            snapshot = await graph.aget_state(parent_thread_config(session_id))
            human_messages = [
                message
                for message in snapshot.values["messages"]
                if isinstance(message, HumanMessage)
            ]
            ai_messages = [
                message
                for message in snapshot.values["messages"]
                if isinstance(message, AIMessage)
            ]
            assert [message.id for message in human_messages] == [
                str(seed_human_id),
                str(submission.input_message_id),
            ]
            assert [message.id for message in ai_messages] == [
                str(seed_response_id),
                str(submission.response_message_id),
            ]
            assert sum(
                message.id == str(submission.input_message_id)
                for message in human_messages
            ) == 1
            async with open_database_connection(settings) as connection:
                cursor = await connection.execute(
                    "SELECT COUNT(*) AS total FROM runs WHERE session_id = %s",
                    (session_id,),
                )
                count = await cursor.fetchone()
            assert count["total"] == 1
            public_events = await events.list_public(
                run_id=submission.run_id,
                after_seq=0,
                limit=100,
            )
            assert [event.event_type for event in public_events] == [
                "run.started",
                "message.started",
                "interrupt.required",
                "interrupt.resumed",
                "message.started",
                "message.finalized",
                "run.completed",
            ]
            assert [
                event.payload["attempt"]
                for event in public_events
                if event.event_type == "message.started"
            ] == [1, 2]
        finally:
            await coordinator.close()
            async with open_database_connection(settings) as connection:
                await connection.execute(
                    "DELETE FROM run_interrupts WHERE run_id = %s",
                    (submission.run_id,),
                )
                await connection.execute(
                    "DELETE FROM runtime_events WHERE run_id = %s",
                    (submission.run_id,),
                )
                await connection.execute(
                    "DELETE FROM runs WHERE run_id = %s",
                    (submission.run_id,),
                )
                await connection.execute(
                    "DELETE FROM sessions WHERE session_id = %s",
                    (session_id,),
                )
                await connection.commit()

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(exercise())
