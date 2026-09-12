"""Stage 1 Parent Graph skeleton."""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast
from uuid import UUID

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    message_chunk_to_message,
)
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Checkpointer

from agent_runtime.graph.state import ParentState

type ParentNode = Callable[
    [ParentState, RunnableConfig],
    Awaitable[dict[str, Any]],
]
type ChildMessageEvent = tuple[BaseMessage, dict[str, Any]]


async def forward_child_message_stream(
    events: AsyncIterator[ChildMessageEvent],
    *,
    message_id: UUID,
) -> AIMessage:
    """Forward Child tokens and assemble the final public Parent message."""

    writer = get_stream_writer()
    final_chunk: AIMessageChunk | None = None
    async for message, _metadata in events:
        writer(message)
        if isinstance(message, AIMessageChunk):
            final_chunk = message if final_chunk is None else final_chunk + message

    if final_chunk is None:
        return AIMessage(content="", id=str(message_id))

    final_message = message_chunk_to_message(final_chunk)
    return cast(AIMessage, final_message.model_copy(update={"id": str(message_id)}))


def build_parent_graph(
    *,
    route: ParentNode,
    invoke_capability: ParentNode,
    checkpointer: Checkpointer = None,
):
    """Compile the fixed Stage 1 Parent control-flow skeleton."""

    async def route_new_request(
        state: ParentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        request_state = cast(
            ParentState,
            {**state, "rejected_capability_ids": []},
        )
        update = await route(request_state, config)
        return {**update, "rejected_capability_ids": []}

    builder = StateGraph(ParentState)
    builder.add_node("route", route_new_request)
    builder.add_node("invoke_capability", invoke_capability)
    builder.add_edge(START, "route")
    builder.add_edge("route", "invoke_capability")
    builder.add_edge("invoke_capability", END)
    return builder.compile(checkpointer=checkpointer)
