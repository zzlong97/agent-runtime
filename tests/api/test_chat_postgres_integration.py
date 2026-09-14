import asyncio
import json
import os
from uuid import UUID

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


class StageOneStreamingFakeModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "stage-one-streaming-fake"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        system_content = str(messages[0].content)
        if "意图路由器" in system_content:
            name = "RouterDecision"
            arguments = {"capability_id": "general_chat", "confidence": 0.99}
        elif "general_chat 的能力边界" in system_content:
            name = "GeneralChatScopeDecision"
            arguments = {"is_translation_request": False}
        else:
            raise AssertionError("测试模型收到了非预期的结构化调用")
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": name,
                                "args": arguments,
                                "id": "structured-call",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )

    def _stream(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ):
        yield ChatGenerationChunk(message=AIMessageChunk(content="测试"))
        yield ChatGenerationChunk(message=AIMessageChunk(content="回复"))


class StageOnePartiallyFailingFakeModel(StageOneStreamingFakeModel):
    @property
    def _llm_type(self) -> str:
        return "stage-one-partially-failing-fake"

    def _stream(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ):
        yield ChatGenerationChunk(message=AIMessageChunk(content="部分输出"))
        raise RuntimeError("provider disconnected")


def run_on_psycopg_compatible_loop(coroutine):
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coroutine)


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
)
def test_chat_endpoint_streams_and_persists_public_messages_in_postgres() -> None:
    from agent_runtime.chat import open_chat_service
    from agent_runtime.core.config import Settings
    from agent_runtime.main import create_app
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.sessions.repository import PostgresSessionRepository

    settings = Settings()
    session_id: UUID | None = None
    history_human_message_id: str | None = None

    async def exercise() -> None:
        nonlocal history_human_message_id, session_id
        try:
            async with open_chat_service(
                settings,
                model=StageOneStreamingFakeModel(),
            ) as service:
                app = create_app(settings, chat_service=service)
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                ) as client:
                    response = await client.post(
                        "/api/v1/chat/completions",
                        json={"message": {"content": "介绍一下测试运行时"}},
                    )

                frames = response.text.strip().split("\n\n")
                events = [
                    (
                        frame.splitlines()[0].removeprefix("event: "),
                        json.loads(
                            frame.splitlines()[1].removeprefix("data: ")
                        ),
                    )
                    for frame in frames
                ]
                session_id = UUID(events[0][1]["session_id"])
                message_id = UUID(events[0][1]["message_id"])

                assert response.status_code == 200
                assert [name for name, _data in events] == [
                    "message",
                    "message",
                    "done",
                ]
                assert [data["delta"] for _name, data in events[:2]] == [
                    "测试",
                    "回复",
                ]
                assert events[-1][1]["status"] == "completed"
                assert {data["message_id"] for _name, data in events} == {
                    str(message_id)
                }
                assert {data["capability_id"] for _name, data in events} == {
                    "general_chat"
                }

                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                ) as history_client:
                    history_response = await history_client.get(
                        f"/api/v1/chat/sessions/{session_id}/messages"
                    )
                assert history_response.status_code == 200
                history_body = history_response.json()
                assert history_body["next_before"] is None
                assert len(history_body["items"]) == 2
                history_human_message_id = history_body["items"][0][
                    "message_id"
                ]
                assert str(UUID(history_human_message_id)) == (
                    history_human_message_id
                )
                assert history_body["items"][0] == {
                    "message_id": history_human_message_id,
                    "role": "user",
                    "content": "介绍一下测试运行时",
                    "runtime_status": None,
                    "capability_id": None,
                    "feedback": None,
                }
                assert history_body["items"][1] == {
                    "message_id": str(message_id),
                    "role": "assistant",
                    "content": "测试回复",
                    "runtime_status": "completed",
                    "capability_id": "general_chat",
                    "feedback": None,
                }

                restored_session = await PostgresSessionRepository(settings).get(
                    session_id
                )
                assert restored_session.user_id == settings.local_user_id
                assert restored_session.title == "介绍一下测试运行时"
                assert (
                    restored_session.updated_at
                    > restored_session.created_at
                )

            assert session_id is not None
            async with AsyncPostgresSaver.from_conn_string(
                settings.database_connection_string
            ) as checkpointer:
                parent_checkpoint = await checkpointer.aget_tuple(
                    {"configurable": {"thread_id": str(session_id)}}
                )
                assert parent_checkpoint is not None
                parent_messages = parent_checkpoint.checkpoint["channel_values"][
                    "messages"
                ]
                assert [message.content for message in parent_messages] == [
                    "介绍一下测试运行时",
                    "测试回复",
                ]
                assert parent_messages[-1].id == str(message_id)
                assert parent_messages[-1].additional_kwargs == {
                    "runtime_status": "completed",
                    "capability_id": "general_chat",
                }
                assert parent_messages[0].id is not None
                assert str(UUID(parent_messages[0].id)) == parent_messages[0].id
                assert parent_messages[0].id == history_human_message_id
        finally:
            if session_id is not None:
                async with open_database_connection(settings) as connection:
                    await connection.execute(
                        "DELETE FROM sessions WHERE session_id = %s",
                        (session_id,),
                    )
                    await connection.commit()
                async with AsyncPostgresSaver.from_conn_string(
                    settings.database_connection_string
                ) as checkpointer:
                    await checkpointer.adelete_thread(str(session_id))
                    await checkpointer.adelete_thread(f"{session_id}:general_chat")
                    await checkpointer.adelete_thread(f"{session_id}:en_to_zh")

    run_on_psycopg_compatible_loop(exercise())


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
)
def test_stop_persists_stopped_message_and_allows_immediate_next_run() -> None:
    from langgraph.config import get_stream_writer

    from agent_runtime.chat import ChatService
    from agent_runtime.core.config import Settings
    from agent_runtime.graph.child_result import CapabilityInvocation, ChildResult
    from agent_runtime.graph.parent import build_parent_graph
    from agent_runtime.persistence.database import open_database_connection
    from agent_runtime.persistence.parent_state import PostgresParentStateStore
    from agent_runtime.sessions.repository import PostgresSessionRepository
    from agent_runtime.sessions.service import SessionService

    settings = Settings()
    session_id: UUID | None = None

    async def exercise() -> None:
        nonlocal session_id
        session_repository = PostgresSessionRepository(settings)
        parent_state_store = PostgresParentStateStore(settings)
        await session_repository.setup()
        await parent_state_store.setup()
        session_service = SessionService(
            settings=settings,
            session_repository=session_repository,
            parent_state_store=parent_state_store,
        )
        started = await session_service.prepare_new_session(content="等待停止")
        session_id = started.session.session_id

        try:
            async with AsyncPostgresSaver.from_conn_string(
                settings.database_connection_string
            ) as checkpointer:
                async def route(state, config):
                    return {"resolved_capability_id": "general_chat"}

                async def invoke_capability(state, config):
                    writer = get_stream_writer()
                    writer(
                        AIMessageChunk(
                            content="已输出部分",
                            additional_kwargs={
                                "capability_id": "general_chat"
                            },
                        )
                    )
                    await asyncio.Event().wait()
                    return CapabilityInvocation(
                        result=ChildResult(
                            status="completed",
                            control_signal=None,
                        ),
                        message=AIMessage(content="不会到达"),
                    )

                parent_graph = build_parent_graph(
                    route=route,
                    invoke_capability=invoke_capability,
                    checkpointer=checkpointer,
                )
                service = ChatService(
                    settings=settings,
                    session_repository=session_repository,
                    session_service=session_service,
                    parent_graph=parent_graph,
                )
                turn = await service.prepare_turn(
                    session_id=session_id,
                    content="请开始长任务",
                )
                await service.start_producer(turn)
                message_event = await turn.active_run.event_queue.get()
                assert message_event is not None
                assert message_event.name == "message"

                assert await service.stop_session(
                    session_id=session_id
                ) == "stopped"
                done_event = await turn.active_run.event_queue.get()
                assert done_event is not None
                assert done_event.name == "done"
                assert done_event.data.status == "stopped"
                assert await turn.active_run.event_queue.get() is None

                parent_state = await parent_graph.aget_state(turn.config)
                parent_messages = parent_state.values["messages"]
                assert [message.content for message in parent_messages][-2:] == [
                    "请开始长任务",
                    "已输出部分",
                ]
                assert parent_messages[-1].id == str(turn.response_message_id)
                assert parent_messages[-1].additional_kwargs == {
                    "runtime_status": "stopped",
                    "capability_id": "general_chat",
                }

                next_turn = await service.prepare_turn(
                    session_id=session_id,
                    content="停止后立即继续",
                )
                assert await service.cancel_run(
                    next_turn.active_run,
                    reason="disconnected",
                ) is True
        finally:
            if session_id is not None:
                async with open_database_connection(settings) as connection:
                    await connection.execute(
                        "DELETE FROM sessions WHERE session_id = %s",
                        (session_id,),
                    )
                    await connection.commit()
                async with AsyncPostgresSaver.from_conn_string(
                    settings.database_connection_string
                ) as checkpointer:
                    await checkpointer.adelete_thread(str(session_id))
                    await checkpointer.adelete_thread(
                        f"{session_id}:general_chat"
                    )
                    await checkpointer.adelete_thread(f"{session_id}:en_to_zh")

    run_on_psycopg_compatible_loop(exercise())


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
)
def test_chat_endpoint_persists_partial_output_as_incomplete_in_postgres() -> None:
    from agent_runtime.chat import open_chat_service
    from agent_runtime.core.config import Settings
    from agent_runtime.main import create_app
    from agent_runtime.persistence.database import open_database_connection

    settings = Settings()
    session_id: UUID | None = None

    async def exercise() -> None:
        nonlocal session_id
        try:
            async with open_chat_service(
                settings,
                model=StageOnePartiallyFailingFakeModel(),
            ) as service:
                app = create_app(settings, chat_service=service)
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                ) as client:
                    response = await client.post(
                        "/api/v1/chat/completions",
                        json={"message": {"content": "触发部分输出失败"}},
                    )

                frames = response.text.strip().split("\n\n")
                events = [
                    (
                        frame.splitlines()[0].removeprefix("event: "),
                        json.loads(
                            frame.splitlines()[1].removeprefix("data: ")
                        ),
                    )
                    for frame in frames
                ]
                session_id = UUID(events[0][1]["session_id"])
                message_id = UUID(events[0][1]["message_id"])

                assert [name for name, _data in events] == [
                    "message",
                    "error",
                    "done",
                ]
                assert events[1][1]["code"] == "GENERAL_CHAT_CALL_FAILED"
                assert events[2][1]["status"] == "failed"

            assert session_id is not None
            async with AsyncPostgresSaver.from_conn_string(
                settings.database_connection_string
            ) as checkpointer:
                parent_checkpoint = await checkpointer.aget_tuple(
                    {"configurable": {"thread_id": str(session_id)}}
                )
                assert parent_checkpoint is not None
                parent_messages = parent_checkpoint.checkpoint["channel_values"][
                    "messages"
                ]
                assert [message.content for message in parent_messages] == [
                    "触发部分输出失败",
                    "部分输出",
                ]
                assert parent_messages[-1].id == str(message_id)
                assert parent_messages[-1].additional_kwargs["runtime_status"] == (
                    "incomplete"
                )
                assert parent_checkpoint.pending_writes == []
        finally:
            if session_id is not None:
                async with open_database_connection(settings) as connection:
                    await connection.execute(
                        "DELETE FROM sessions WHERE session_id = %s",
                        (session_id,),
                    )
                    await connection.commit()
                async with AsyncPostgresSaver.from_conn_string(
                    settings.database_connection_string
                ) as checkpointer:
                    await checkpointer.adelete_thread(str(session_id))
                    await checkpointer.adelete_thread(f"{session_id}:general_chat")
                    await checkpointer.adelete_thread(f"{session_id}:en_to_zh")

    run_on_psycopg_compatible_loop(exercise())
