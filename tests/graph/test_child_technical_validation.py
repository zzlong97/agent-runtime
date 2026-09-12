import asyncio
from collections.abc import Iterator
from typing import TypedDict
from uuid import uuid4

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    HumanMessage,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph


class TranslationProbeState(TypedDict):
    messages: list[AnyMessage]
    draft_translation: str


def test_parent_invokes_create_agent_child_and_forwards_tokens() -> None:
    from agent_runtime.graph.config import (
        child_thread_config,
        parent_thread_config,
    )
    from agent_runtime.graph.parent import (
        build_parent_graph,
        forward_child_message_stream,
    )
    from agent_runtime.graph.state import ParentState

    session_id = uuid4()
    parent_config = parent_thread_config(session_id)
    child_config = child_thread_config(session_id, "general_chat")
    message_id = uuid4()
    parent_checkpointer = InMemorySaver()
    child_checkpointer = InMemorySaver()
    model_messages: Iterator[AIMessage | str] = iter(
        [AIMessage(content="hello world")]
    )
    child = create_agent(
        model=GenericFakeChatModel(messages=model_messages),
        tools=[],
        checkpointer=child_checkpointer,
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

    parent = build_parent_graph(
        route=route,
        invoke_capability=invoke_capability,
        checkpointer=parent_checkpointer,
    )

    async def exercise():
        tokens = [
            event
            async for event in parent.astream(
                {
                    "messages": [HumanMessage(content="hi")],
                    "resolved_capability_id": None,
                    "rejected_capability_ids": [],
                },
                parent_config,
                stream_mode="custom",
            )
        ]
        return (
            tokens,
            await parent_checkpointer.aget_tuple(parent_config),
            await child_checkpointer.aget_tuple(child_config),
        )

    tokens, parent_checkpoint, child_checkpoint = asyncio.run(exercise())

    assert all(isinstance(token, AIMessageChunk) for token in tokens)
    assert [token.content for token in tokens] == ["hello", " ", "world"]
    assert child.builder.state_schema is not ParentState
    assert child.builder.state_schema is not TranslationProbeState
    assert parent_config != child_config
    assert parent_checkpoint is not None
    assert child_checkpoint is not None
    parent_messages = parent_checkpoint.checkpoint["channel_values"]["messages"]
    assert isinstance(parent_messages[-1], AIMessage)
    assert parent_messages[-1].content == "hello world"
    assert parent_messages[-1].id == str(message_id)


def test_parent_invokes_state_graph_child_without_leaking_private_state() -> None:
    from agent_runtime.graph.config import (
        child_thread_config,
        parent_thread_config,
    )
    from agent_runtime.graph.parent import (
        build_parent_graph,
        forward_child_message_stream,
    )
    from agent_runtime.graph.state import ParentState

    session_id = uuid4()
    parent_config = parent_thread_config(session_id)
    child_config = child_thread_config(session_id, "en_to_zh")
    message_id = uuid4()
    parent_checkpointer = InMemorySaver()
    child_checkpointer = InMemorySaver()
    model_messages: Iterator[AIMessage | str] = iter(
        [AIMessage(content="translated output")]
    )
    model = GenericFakeChatModel(messages=model_messages)

    async def generate_translation(state: TranslationProbeState):
        response = await model.ainvoke(state["messages"])
        return {"draft_translation": response.content}

    child_builder = StateGraph(TranslationProbeState)
    child_builder.add_node("generate_translation", generate_translation)
    child_builder.add_edge(START, "generate_translation")
    child_builder.add_edge("generate_translation", END)
    child = child_builder.compile(checkpointer=child_checkpointer)

    async def route(state, config):
        return {"resolved_capability_id": "en_to_zh"}

    async def invoke_capability(state, config):
        final_message = await forward_child_message_stream(
            child.astream(
                {
                    "messages": state["messages"],
                    "draft_translation": "",
                },
                child_config,
                stream_mode="messages",
            ),
            message_id=message_id,
        )
        return {"messages": [final_message]}

    parent = build_parent_graph(
        route=route,
        invoke_capability=invoke_capability,
        checkpointer=parent_checkpointer,
    )

    async def exercise():
        tokens = [
            event
            async for event in parent.astream(
                {
                    "messages": [HumanMessage(content="translate me")],
                    "resolved_capability_id": None,
                    "rejected_capability_ids": [],
                },
                parent_config,
                stream_mode="custom",
            )
        ]
        return (
            tokens,
            await parent_checkpointer.aget_tuple(parent_config),
            await child_checkpointer.aget_tuple(child_config),
        )

    tokens, parent_checkpoint, child_checkpoint = asyncio.run(exercise())

    assert all(isinstance(token, AIMessageChunk) for token in tokens)
    assert "".join(str(token.content) for token in tokens) == "translated output"
    assert child.builder.state_schema is TranslationProbeState
    assert child.builder.state_schema is not ParentState
    assert parent_config != child_config
    assert parent_checkpoint is not None
    assert child_checkpoint is not None
    assert "draft_translation" not in parent_checkpoint.checkpoint["channel_values"]
    parent_messages = parent_checkpoint.checkpoint["channel_values"]["messages"]
    assert isinstance(parent_messages[-1], AIMessage)
    assert parent_messages[-1].content == "translated output"
    assert parent_messages[-1].id == str(message_id)
    assert (
        child_checkpoint.checkpoint["channel_values"]["draft_translation"]
        == "translated output"
    )
