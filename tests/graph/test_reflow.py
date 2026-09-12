import asyncio
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver


def test_parent_continues_current_capability_without_rerouting() -> None:
    from agent_runtime.graph.child_result import CapabilityInvocation, ChildResult
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.parent import build_parent_graph

    route_calls = 0
    invoked_capabilities: list[str | None] = []

    async def route(state, config):
        nonlocal route_calls
        route_calls += 1
        return {"resolved_capability_id": "en_to_zh"}

    async def invoke_capability(state, config):
        invoked_capabilities.append(state["resolved_capability_id"])
        return CapabilityInvocation(
            result=ChildResult(status="completed", control_signal=None),
            message=AIMessage(content="普通聊天回复", id=str(uuid4())),
        )

    graph = build_parent_graph(
        route=route,
        invoke_capability=invoke_capability,
    )
    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="继续聊")],
                "resolved_capability_id": "general_chat",
                "rejected_capability_ids": ["en_to_zh"],
            }
        )
    )

    assert route_calls == 0
    assert invoked_capabilities == ["general_chat"]
    assert result["completion_status"] == "completed"
    assert result["rejected_capability_ids"] == []


@pytest.mark.parametrize(
    ("current_capability", "next_capability", "reply"),
    [
        ("general_chat", "en_to_zh", "你好吗？"),
        ("en_to_zh", "general_chat", "FastAPI 是一个 Web 框架。"),
    ],
)
def test_parent_switches_capability_after_out_of_scope(
    current_capability: str,
    next_capability: str,
    reply: str,
) -> None:
    from agent_runtime.graph.child_result import CapabilityInvocation, ChildResult
    from agent_runtime.graph.parent import build_parent_graph

    route_rejections: list[list[str]] = []
    invoked_capabilities: list[str | None] = []

    async def route(state, config):
        route_rejections.append(list(state["rejected_capability_ids"]))
        assert current_capability in state["rejected_capability_ids"]
        return {"resolved_capability_id": next_capability}

    async def invoke_capability(state, config):
        capability_id = state["resolved_capability_id"]
        invoked_capabilities.append(capability_id)
        if capability_id == current_capability:
            return CapabilityInvocation(
                result=ChildResult(
                    status="rejected",
                    control_signal="OUT_OF_SCOPE",
                )
            )
        return CapabilityInvocation(
            result=ChildResult(status="completed", control_signal=None),
            message=AIMessage(content=reply, id=str(uuid4())),
        )

    human_message = HumanMessage(content="当前请求", id=str(uuid4()))
    graph = build_parent_graph(
        route=route,
        invoke_capability=invoke_capability,
    )
    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [human_message],
                "resolved_capability_id": current_capability,
                "rejected_capability_ids": [],
            }
        )
    )

    assert invoked_capabilities == [current_capability, next_capability]
    assert route_rejections == [[current_capability]]
    assert result["resolved_capability_id"] == next_capability
    assert result["completion_status"] == "completed"
    assert [message.content for message in result["messages"]] == [
        "当前请求",
        reply,
    ]
    assert sum(message.id == human_message.id for message in result["messages"]) == 1


def test_all_rejected_capabilities_emit_and_persist_unsupported_message() -> None:
    from agent_runtime.graph.child_result import CapabilityInvocation, ChildResult
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.parent import UNSUPPORTED_REPLY, build_parent_graph

    route_rejections: list[list[str]] = []
    invoked_capabilities: list[str | None] = []
    unsupported_message_id = UUID("00000000-0000-0000-0000-000000000321")
    fallback_message_id = UUID("00000000-0000-0000-0000-000000000322")
    saver = InMemorySaver()
    config = parent_thread_config(uuid4(), message_id=unsupported_message_id)

    async def route(state, config):
        rejected = list(state["rejected_capability_ids"])
        route_rejections.append(rejected)
        available = [
            capability_id
            for capability_id in ("general_chat", "en_to_zh")
            if capability_id not in rejected
        ]
        return {"resolved_capability_id": available[0]}

    async def invoke_capability(state, config):
        invoked_capabilities.append(state["resolved_capability_id"])
        return CapabilityInvocation(
            result=ChildResult(
                status="rejected",
                control_signal="OUT_OF_SCOPE",
            )
        )

    graph = build_parent_graph(
        route=route,
        invoke_capability=invoke_capability,
        checkpointer=saver,
        message_id_factory=lambda: fallback_message_id,
    )
    human_message = HumanMessage(content="把你好翻译成英文", id=str(uuid4()))

    async def exercise():
        events = [
            event
            async for event in graph.astream(
                {
                    "messages": [human_message],
                    "resolved_capability_id": None,
                    "rejected_capability_ids": [],
                },
                config,
                stream_mode="custom",
            )
        ]
        state = await graph.aget_state(config)
        return events, state.values

    events, result = asyncio.run(exercise())

    assert route_rejections == [[], ["general_chat"]]
    assert invoked_capabilities == ["general_chat", "en_to_zh"]
    assert result["rejected_capability_ids"] == ["general_chat", "en_to_zh"]
    assert result["resolved_capability_id"] is None
    assert result["completion_status"] == "unsupported"
    assert [message.content for message in result["messages"]] == [
        "把你好翻译成英文",
        UNSUPPORTED_REPLY,
    ]
    assert sum(message.id == human_message.id for message in result["messages"]) == 1
    assert len(events) == 1
    assert isinstance(events[0], AIMessage)
    assert events[0].content == UNSUPPORTED_REPLY
    assert events[0].id == str(unsupported_message_id)
    assert events[0].additional_kwargs["runtime_status"] == "unsupported"


def test_failed_child_result_is_an_error_not_unsupported() -> None:
    from agent_runtime.graph.child_result import CapabilityInvocation, ChildResult
    from agent_runtime.graph.parent import ParentGraphError, build_parent_graph

    async def route(state, config):
        return {"resolved_capability_id": "general_chat"}

    async def invoke_capability(state, config):
        return CapabilityInvocation(
            result=ChildResult(status="failed", control_signal=None)
        )

    graph = build_parent_graph(
        route=route,
        invoke_capability=invoke_capability,
    )

    with pytest.raises(ParentGraphError) as captured:
        asyncio.run(
            graph.ainvoke(
                {
                    "messages": [HumanMessage(content="你好")],
                    "resolved_capability_id": None,
                    "rejected_capability_ids": [],
                }
            )
        )

    assert captured.value.code == "CHILD_EXECUTION_FAILED"
    assert captured.value.message == "Child Agent 返回了执行失败状态"
