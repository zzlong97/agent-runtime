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

    async def exercise() -> None:
        nonlocal session_id
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
                assert {data["message_id"] for _name, data in events[:2]} == {
                    str(message_id)
                }

                restored_session = await PostgresSessionRepository(settings).get(
                    session_id
                )
                assert restored_session.user_id == settings.local_user_id
                assert restored_session.title == "介绍一下测试运行时"

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
                assert parent_messages[0].id is not None
                assert str(UUID(parent_messages[0].id)) == parent_messages[0].id
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
