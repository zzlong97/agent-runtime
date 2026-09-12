import asyncio
import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


def run_on_psycopg_compatible_loop(coroutine):
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coroutine)


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
)
def test_fresh_parent_graph_restores_child_ai_message_from_postgres() -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.graph.config import (
        child_thread_config,
        parent_thread_config,
    )
    from agent_runtime.graph.parent import (
        build_parent_graph,
        forward_child_message_stream,
    )

    settings = Settings()
    session_id = uuid4()
    message_id = uuid4()
    parent_config = parent_thread_config(session_id)
    child_config = child_thread_config(session_id, "general_chat")

    async def exercise() -> None:
        try:
            async with AsyncPostgresSaver.from_conn_string(
                settings.database_connection_string
            ) as first_parent_checkpointer:
                await first_parent_checkpointer.setup()
                model_messages: Iterator[AIMessage | str] = iter(
                    [AIMessage(content="persisted response")]
                )
                child = create_agent(
                    model=GenericFakeChatModel(messages=model_messages),
                    tools=[],
                    checkpointer=InMemorySaver(),
                )

                async def route(state, config):
                    return {"resolved_capability_id": "general_chat"}

                async def invoke_capability(state, config):
                    final_message = await forward_child_message_stream(
                        child.astream(
                            {"messages": state["messages"]},
                            child_config,
                            stream_mode="messages",
                        ),
                        message_id=message_id,
                    )
                    return {"messages": [final_message]}

                first_parent = build_parent_graph(
                    route=route,
                    invoke_capability=invoke_capability,
                    checkpointer=first_parent_checkpointer,
                )
                tokens = [
                    event
                    async for event in first_parent.astream(
                        {
                            "messages": [HumanMessage(content="remember this")],
                            "resolved_capability_id": None,
                            "rejected_capability_ids": [],
                        },
                        parent_config,
                        stream_mode="custom",
                    )
                ]

            async with AsyncPostgresSaver.from_conn_string(
                settings.database_connection_string
            ) as restarted_parent_checkpointer:
                restarted_parent = build_parent_graph(
                    route=route,
                    invoke_capability=invoke_capability,
                    checkpointer=restarted_parent_checkpointer,
                )
                restored = await restarted_parent.aget_state(parent_config)

            restored_messages = restored.values["messages"]
            assert "".join(str(token.content) for token in tokens) == (
                "persisted response"
            )
            assert [message.content for message in restored_messages] == [
                "remember this",
                "persisted response",
            ]
            assert restored_messages[-1].id == str(message_id)
        finally:
            async with AsyncPostgresSaver.from_conn_string(
                settings.database_connection_string
            ) as cleanup_checkpointer:
                await cleanup_checkpointer.adelete_thread(str(session_id))

    run_on_psycopg_compatible_loop(exercise())
