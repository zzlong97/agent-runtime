"""Stage 1 Parent Graph 的固定调度与有限回流控制。"""

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    message_chunk_to_message,
)
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Checkpointer, Command

from agent_runtime.core.errors import ApplicationError
from agent_runtime.graph.child_result import CapabilityInvocation
from agent_runtime.graph.config import public_message_id_from_parent_config
from agent_runtime.graph.state import ParentState

UNSUPPORTED_REPLY = "抱歉，当前能力无法处理这个请求。"
_CAPABILITY_IDS = ("general_chat", "en_to_zh")


class ParentGraphError(ApplicationError):
    """Parent Graph 收到非法控制结果或无法继续调度。"""


type ParentNode = Callable[
    [ParentState, RunnableConfig],
    Awaitable[dict[str, Any]],
]
type CapabilityNode = Callable[
    [ParentState, RunnableConfig],
    Awaitable[CapabilityInvocation],
]
type ChildMessageEvent = tuple[BaseMessage, dict[str, Any]]


async def forward_child_message_stream(
    events: AsyncIterator[ChildMessageEvent],
    *,
    message_id: UUID,
) -> AIMessage:
    """转发 Child 消息事件并聚合最终公共 AIMessage。"""

    writer = get_stream_writer()
    emitted_messages: list[BaseMessage] = []
    async for message, _metadata in events:
        writer(message)
        emitted_messages.append(message)

    return build_public_ai_message(emitted_messages, message_id=message_id)


def build_public_ai_message(
    emitted_messages: Iterable[BaseMessage],
    *,
    message_id: UUID,
) -> AIMessage:
    """把 Child 消息事件聚合为带服务端稳定 UUID 的完整公共 AIMessage。"""

    final_chunk: AIMessageChunk | None = None
    final_message: AIMessage | None = None
    for message in emitted_messages:
        if isinstance(message, AIMessageChunk):
            final_chunk = message if final_chunk is None else final_chunk + message
        elif isinstance(message, AIMessage):
            final_message = message

    if final_chunk is not None:
        final_message = cast(AIMessage, message_chunk_to_message(final_chunk))
    if final_message is None:
        final_message = AIMessage(content="")

    return cast(AIMessage, final_message.model_copy(update={"id": str(message_id)}))


def build_parent_graph(
    *,
    route: ParentNode,
    invoke_capability: CapabilityNode,
    checkpointer: Checkpointer = None,
    message_id_factory: Callable[[], UUID] = uuid4,
):
    """编译支持当前能力续用和有限 OUT_OF_SCOPE 回流的 Parent Graph。"""

    async def prepare_request(
        state: ParentState,
    ) -> dict[str, Any]:
        """在每个新 HumanMessage 开始时重置本轮控制状态。"""

        return {
            "rejected_capability_ids": [],
            "completion_status": None,
        }

    def choose_initial_step(
        state: ParentState,
    ) -> Literal["route", "invoke_capability"]:
        """已有当前能力时直接执行，否则先进行首次路由。"""

        if state.get("resolved_capability_id") is None:
            return "route"
        return "invoke_capability"

    async def route_capability(
        state: ParentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        """调用 Router，并禁止选择本轮已经拒绝的能力。"""

        update = await route(state, config)
        capability_id = update.get("resolved_capability_id")
        if capability_id not in _CAPABILITY_IDS:
            raise ParentGraphError(
                code="PARENT_INVALID_CAPABILITY",
                message="Router 未返回 Stage 1 允许的能力",
            )
        if capability_id in state["rejected_capability_ids"]:
            raise ParentGraphError(
                code="PARENT_RESELECTED_CAPABILITY",
                message="Router 选择了本轮已经拒绝的能力",
            )
        return update

    async def invoke_selected_capability(
        state: ParentState,
        config: RunnableConfig,
    ) -> Command[Literal["route", "unsupported", "__end__"]]:
        """消费标准 ChildResult，并决定完成、重路由或 unsupported。"""

        capability_id = state.get("resolved_capability_id")
        if capability_id not in _CAPABILITY_IDS:
            raise ParentGraphError(
                code="PARENT_INVALID_CAPABILITY",
                message="Parent Graph 缺少可调用的 Stage 1 能力",
            )
        if capability_id in state["rejected_capability_ids"]:
            raise ParentGraphError(
                code="PARENT_CAPABILITY_ALREADY_REJECTED",
                message="同一 HumanMessage 不能重复调用已经拒绝的能力",
            )

        invocation = await invoke_capability(state, config)
        result = invocation.result
        if result.status == "completed":
            if invocation.message is None:
                raise ParentGraphError(
                    code="PARENT_MISSING_PUBLIC_MESSAGE",
                    message="Child Agent 完成后未提供最终公共 AIMessage",
                )
            return Command(
                update={
                    "messages": [invocation.message],
                    "completion_status": "completed",
                },
                goto=END,
            )

        if result.status == "failed":
            raise ParentGraphError(
                code="CHILD_EXECUTION_FAILED",
                message="Child Agent 返回了执行失败状态",
            )

        if invocation.message is not None:
            raise ParentGraphError(
                code="PARENT_REJECTED_WITH_MESSAGE",
                message="Child Agent 拒绝任务时不得提供用户可见消息",
            )
        rejected_capability_ids = [*state["rejected_capability_ids"]]
        rejected_capability_ids.append(capability_id)
        remaining_capability_ids = [
            candidate
            for candidate in _CAPABILITY_IDS
            if candidate not in rejected_capability_ids
        ]
        next_node = "route" if remaining_capability_ids else "unsupported"
        return Command(
            update={
                "resolved_capability_id": None,
                "rejected_capability_ids": rejected_capability_ids,
            },
            goto=next_node,
        )

    async def generate_unsupported_reply(
        state: ParentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        """生成固定 unsupported 回复并作为普通消息事件发送。"""

        message_id = public_message_id_from_parent_config(config)
        message = AIMessage(
            content=UNSUPPORTED_REPLY,
            id=str(message_id or message_id_factory()),
            additional_kwargs={
                "runtime_status": "unsupported",
                "capability_id": None,
            },
        )
        writer = get_stream_writer()
        writer(message)
        return {
            "messages": [message],
            "resolved_capability_id": None,
            "completion_status": "unsupported",
        }

    builder = StateGraph(ParentState)
    builder.add_node("prepare_request", prepare_request)
    builder.add_node("route", route_capability)
    builder.add_node("invoke_capability", invoke_selected_capability)
    builder.add_node("unsupported", generate_unsupported_reply)
    builder.add_edge(START, "prepare_request")
    builder.add_conditional_edges("prepare_request", choose_initial_step)
    builder.add_edge("route", "invoke_capability")
    builder.add_edge("unsupported", END)
    return builder.compile(checkpointer=checkpointer)
