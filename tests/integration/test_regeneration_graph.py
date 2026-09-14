"""使用真实 Parent Graph 和 InMemorySaver 验证重新生成分支。"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer


class InMemorySessionRepository:
    def __init__(self, session) -> None:
        self.session = session

    async def get(self, session_id: UUID):
        return self.session


class GraphParentStateStore:
    def __init__(self, graph) -> None:
        self.graph = graph

    async def get_messages(self, session_id: UUID):
        from agent_runtime.graph.config import parent_thread_config

        state = await self.graph.aget_state(parent_thread_config(session_id))
        return list(state.values["messages"])


class RecordingSessionService:
    def __init__(self) -> None:
        self.touched: list[UUID] = []

    async def touch_session(self, *, session_id: UUID) -> None:
        self.touched.append(session_id)


def _session(session_id: UUID):
    from agent_runtime.sessions.models import Session

    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    return Session(
        session_id=session_id,
        user_id="configured-user",
        title="重新生成测试",
        created_at=now,
        updated_at=now,
    )


async def _drain_run_events(turn):
    events = []
    while True:
        event = await turn.active_run.event_queue.get()
        if event is None:
            return events
        events.append(event)


def test_regeneration_forks_parent_and_replaces_only_active_answer() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.graph.child_result import CapabilityInvocation, ChildResult
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.parent import build_parent_graph
    from agent_runtime.history import MessageHistoryAdapter

    session_id = UUID("00000000-0000-0000-0000-000000002001")
    human_id = UUID("00000000-0000-0000-0000-000000002002")
    old_message_id = UUID("00000000-0000-0000-0000-000000002003")
    new_message_id = UUID("00000000-0000-0000-0000-000000002004")
    checkpointer = InMemorySaver()
    responses = iter(["原回答", "重新生成的回答"])
    route_calls = 0
    capability_calls = 0

    async def route(state, config):
        nonlocal route_calls
        route_calls += 1
        return {"resolved_capability_id": "general_chat"}

    async def invoke_capability(state, config):
        nonlocal capability_calls
        capability_calls += 1
        content = next(responses)
        get_stream_writer()(
            AIMessageChunk(
                content=content,
                additional_kwargs={"capability_id": "general_chat"},
            )
        )
        return CapabilityInvocation(
            result=ChildResult(status="completed", control_signal=None),
            message=AIMessage(
                content=content,
                id=config["configurable"]["message_id"],
                additional_kwargs={
                    "runtime_status": "completed",
                    "capability_id": "general_chat",
                },
            ),
        )

    graph = build_parent_graph(
        route=route,
        invoke_capability=invoke_capability,
        checkpointer=checkpointer,
    )
    repository = InMemorySessionRepository(_session(session_id))
    history_adapter = MessageHistoryAdapter(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=repository,
        parent_state_store=GraphParentStateStore(graph),
    )
    session_service = RecordingSessionService()
    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=repository,
        session_service=session_service,
        parent_graph=graph,
        history_adapter=history_adapter,
        response_message_id_factory=lambda: new_message_id,
    )

    async def exercise():
        async for _event in graph.astream(
            {
                "messages": [
                    HumanMessage(
                        content="请回答",
                        id=str(human_id),
                    )
                ]
            },
            parent_thread_config(
                session_id,
                message_id=old_message_id,
            ),
            stream_mode="custom",
        ):
            pass

        turn = await service.prepare_regeneration(
            session_id=session_id,
            message_id=old_message_id,
        )
        await service.start_producer(turn)
        events = await _drain_run_events(turn)
        active_messages = await history_adapter.get_active_messages(
            session_id=session_id
        )
        snapshots = [
            snapshot
            async for snapshot in graph.aget_state_history(
                parent_thread_config(session_id)
            )
        ]
        return turn, events, active_messages, snapshots

    turn, events, active_messages, snapshots = asyncio.run(exercise())

    assert [event.name for event in events] == ["message", "done"]
    assert {event.data.message_id for event in events} == {new_message_id}
    assert events[0].data.delta == "重新生成的回答"
    assert events[-1].data.status == "completed"
    assert [message.message_id for message in active_messages] == [
        human_id,
        new_message_id,
    ]
    assert [message.content for message in active_messages] == [
        "请回答",
        "重新生成的回答",
    ]
    assert sum(
        message.message_id == human_id for message in active_messages
    ) == 1
    assert any(
        any(message.id == str(old_message_id) for message in snapshot.values["messages"])
        for snapshot in snapshots
        if "messages" in snapshot.values
    )
    assert route_calls == 1
    assert capability_calls == 2
    assert turn.is_regeneration is True
    assert session_service.touched == [session_id]


def test_failed_regeneration_keeps_new_branch_active_and_old_checkpoint() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.core.errors import ApplicationError
    from agent_runtime.graph.child_result import CapabilityInvocation, ChildResult
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.parent import build_parent_graph
    from agent_runtime.history import MessageHistoryAdapter

    session_id = UUID("00000000-0000-0000-0000-000000002011")
    human_id = UUID("00000000-0000-0000-0000-000000002012")
    old_message_id = UUID("00000000-0000-0000-0000-000000002013")
    new_message_id = UUID("00000000-0000-0000-0000-000000002014")
    checkpointer = InMemorySaver()
    capability_calls = 0

    async def route(state, config):
        return {"resolved_capability_id": "general_chat"}

    async def invoke_capability(state, config):
        nonlocal capability_calls
        capability_calls += 1
        if capability_calls == 2:
            get_stream_writer()(
                AIMessageChunk(
                    content="部分新回答",
                    additional_kwargs={"capability_id": "general_chat"},
                )
            )
            raise ApplicationError(
                code="MODEL_CALL_FAILED",
                message="模型调用失败",
                retryable=True,
            )
        return CapabilityInvocation(
            result=ChildResult(status="completed", control_signal=None),
            message=AIMessage(
                content="原回答",
                id=str(old_message_id),
                additional_kwargs={
                    "runtime_status": "completed",
                    "capability_id": "general_chat",
                },
            ),
        )

    graph = build_parent_graph(
        route=route,
        invoke_capability=invoke_capability,
        checkpointer=checkpointer,
    )
    repository = InMemorySessionRepository(_session(session_id))
    history_adapter = MessageHistoryAdapter(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=repository,
        parent_state_store=GraphParentStateStore(graph),
    )
    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=repository,
        session_service=RecordingSessionService(),
        parent_graph=graph,
        history_adapter=history_adapter,
        response_message_id_factory=lambda: new_message_id,
    )

    async def exercise():
        async for _event in graph.astream(
            {
                "messages": [
                    HumanMessage(content="请回答", id=str(human_id))
                ]
            },
            parent_thread_config(session_id, message_id=old_message_id),
            stream_mode="custom",
        ):
            pass
        turn = await service.prepare_regeneration(
            session_id=session_id,
            message_id=old_message_id,
        )
        await service.start_producer(turn)
        events = await _drain_run_events(turn)
        active_messages = await history_adapter.get_active_messages(
            session_id=session_id
        )
        snapshots = [
            snapshot
            async for snapshot in graph.aget_state_history(
                parent_thread_config(session_id)
            )
        ]
        return events, active_messages, snapshots

    events, active_messages, snapshots = asyncio.run(exercise())

    assert [event.name for event in events] == ["message", "error", "done"]
    assert events[1].data.code == "MODEL_CALL_FAILED"
    assert events[2].data.status == "failed"
    assert [message.message_id for message in active_messages] == [
        human_id,
        new_message_id,
    ]
    assert active_messages[-1].content == "部分新回答"
    assert active_messages[-1].runtime_status == "incomplete"
    assert any(
        any(message.id == str(old_message_id) for message in snapshot.values["messages"])
        for snapshot in snapshots
        if "messages" in snapshot.values
    )


def test_stopped_regeneration_keeps_stopped_branch_active() -> None:
    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.graph.child_result import CapabilityInvocation, ChildResult
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.parent import build_parent_graph
    from agent_runtime.history import MessageHistoryAdapter

    session_id = UUID("00000000-0000-0000-0000-000000002021")
    human_id = UUID("00000000-0000-0000-0000-000000002022")
    old_message_id = UUID("00000000-0000-0000-0000-000000002023")
    new_message_id = UUID("00000000-0000-0000-0000-000000002024")
    checkpointer = InMemorySaver()
    capability_calls = 0
    regeneration_started = asyncio.Event()

    async def route(state, config):
        return {"resolved_capability_id": "general_chat"}

    async def invoke_capability(state, config):
        nonlocal capability_calls
        capability_calls += 1
        if capability_calls == 2:
            get_stream_writer()(
                AIMessageChunk(
                    content="停止前部分",
                    additional_kwargs={"capability_id": "general_chat"},
                )
            )
            regeneration_started.set()
            await asyncio.Event().wait()
        return CapabilityInvocation(
            result=ChildResult(status="completed", control_signal=None),
            message=AIMessage(
                content="原回答",
                id=str(old_message_id),
                additional_kwargs={
                    "runtime_status": "completed",
                    "capability_id": "general_chat",
                },
            ),
        )

    graph = build_parent_graph(
        route=route,
        invoke_capability=invoke_capability,
        checkpointer=checkpointer,
    )
    repository = InMemorySessionRepository(_session(session_id))
    history_adapter = MessageHistoryAdapter(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=repository,
        parent_state_store=GraphParentStateStore(graph),
    )
    service = ChatService(
        settings=Settings(local_user_id="configured-user", _env_file=None),
        session_repository=repository,
        session_service=RecordingSessionService(),
        parent_graph=graph,
        history_adapter=history_adapter,
        response_message_id_factory=lambda: new_message_id,
    )

    async def exercise():
        async for _event in graph.astream(
            {
                "messages": [
                    HumanMessage(content="请回答", id=str(human_id))
                ]
            },
            parent_thread_config(session_id, message_id=old_message_id),
            stream_mode="custom",
        ):
            pass
        turn = await service.prepare_regeneration(
            session_id=session_id,
            message_id=old_message_id,
        )
        await service.start_producer(turn)
        first_event = await turn.active_run.event_queue.get()
        await regeneration_started.wait()
        stop_result = await service.stop_session(session_id=session_id)
        remaining_events = await _drain_run_events(turn)
        active_messages = await history_adapter.get_active_messages(
            session_id=session_id
        )
        return first_event, remaining_events, stop_result, active_messages

    first_event, remaining_events, stop_result, active_messages = asyncio.run(
        exercise()
    )

    assert first_event is not None
    assert first_event.name == "message"
    assert first_event.data.delta == "停止前部分"
    assert [event.name for event in remaining_events] == ["done"]
    assert remaining_events[0].data.status == "stopped"
    assert stop_result == "stopped"
    assert [message.message_id for message in active_messages] == [
        human_id,
        new_message_id,
    ]
    assert active_messages[-1].content == "停止前部分"
    assert active_messages[-1].runtime_status == "stopped"
