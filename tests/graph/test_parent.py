import asyncio
import logging
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver


def test_parent_graph_logs_route_and_capability_boundaries(caplog) -> None:
    from agent_runtime.graph.child_result import CapabilityInvocation, ChildResult
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.parent import build_parent_graph

    session_id = uuid4()

    async def route(state, config):
        return {"resolved_capability_id": "general_chat"}

    async def invoke_capability(state, config):
        return CapabilityInvocation(
            result=ChildResult(status="completed", control_signal=None),
            message=AIMessage(content="回复", id=str(uuid4())),
        )

    graph = build_parent_graph(
        route=route,
        invoke_capability=invoke_capability,
    )

    with caplog.at_level(logging.INFO, logger="agent_runtime.graph.parent"):
        asyncio.run(
            graph.ainvoke(
                {
                    "messages": [HumanMessage(content="不应出现在业务日志中的正文")],
                    "resolved_capability_id": None,
                    "rejected_capability_ids": [],
                },
                parent_thread_config(session_id),
            )
        )

    log_text = "\n".join(caplog.messages)
    assert "Parent路由开始" in log_text
    assert "Parent路由完成" in log_text
    assert "能力调用开始" in log_text
    assert "能力调用完成" in log_text
    assert str(session_id) in log_text
    assert "general_chat" in log_text
    assert "duration_ms" in log_text
    assert "不应出现在业务日志中的正文" not in log_text


def test_parent_graph_logs_capability_exception_with_correlation_ids(caplog) -> None:
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.parent import build_parent_graph

    session_id = uuid4()
    message_id = uuid4()

    async def route(state, config):
        return {"resolved_capability_id": "en_to_zh"}

    async def invoke_capability(state, config):
        raise RuntimeError("能力异常正文不得写入业务日志")

    graph = build_parent_graph(
        route=route,
        invoke_capability=invoke_capability,
    )

    with caplog.at_level(logging.INFO, logger="agent_runtime.graph.parent"):
        with pytest.raises(RuntimeError):
            asyncio.run(
                graph.ainvoke(
                    {
                        "messages": [HumanMessage(content="待翻译正文")],
                        "resolved_capability_id": None,
                        "rejected_capability_ids": [],
                    },
                    parent_thread_config(session_id, message_id=message_id),
                )
            )

    log_text = "\n".join(caplog.messages)
    assert "能力调用异常" in log_text
    assert str(session_id) in log_text
    assert str(message_id) in log_text
    assert "en_to_zh" in log_text
    assert "RuntimeError" in log_text
    assert "能力异常正文不得写入业务日志" not in log_text


def test_parent_graph_runs_the_fixed_stage_one_skeleton() -> None:
    from agent_runtime.graph.child_result import CapabilityInvocation, ChildResult
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.parent import build_parent_graph

    calls: list[str] = []

    async def route(state, config):
        calls.append("route")
        return {"resolved_capability_id": "general_chat"}

    async def invoke_capability(state, config):
        calls.append("invoke_capability")
        assert state["resolved_capability_id"] == "general_chat"
        return CapabilityInvocation(
            result=ChildResult(status="completed", control_signal=None),
            message=AIMessage(content="回复", id=str(uuid4())),
        )

    graph = build_parent_graph(
        route=route,
        invoke_capability=invoke_capability,
    )
    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="hello")],
                "resolved_capability_id": None,
                "rejected_capability_ids": [],
            },
            parent_thread_config(uuid4()),
        )
    )

    assert calls == ["route", "invoke_capability"]
    assert result["resolved_capability_id"] == "general_chat"
    assert set(graph.get_graph().nodes) == {
        "__start__",
        "prepare_request",
        "route",
        "invoke_capability",
        "unsupported",
        "__end__",
    }


def test_parent_graph_resets_rejections_before_each_new_request() -> None:
    from agent_runtime.graph.child_result import CapabilityInvocation, ChildResult
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.parent import build_parent_graph

    rejections_seen_by_route: list[list[str]] = []
    rejections_seen_by_capability: list[list[str]] = []

    async def route(state, config):
        rejections_seen_by_route.append(state["rejected_capability_ids"])
        return {"resolved_capability_id": "general_chat"}

    async def invoke_capability(state, config):
        rejections_seen_by_capability.append(state["rejected_capability_ids"])
        return CapabilityInvocation(
            result=ChildResult(status="completed", control_signal=None),
            message=AIMessage(content="回复", id=str(uuid4())),
        )

    graph = build_parent_graph(
        route=route,
        invoke_capability=invoke_capability,
        checkpointer=InMemorySaver(),
    )
    session_config = parent_thread_config(uuid4())

    async def exercise() -> list[str]:
        await graph.ainvoke(
            {
                "messages": [HumanMessage(content="first")],
                "resolved_capability_id": None,
                "rejected_capability_ids": ["general_chat"],
            },
            session_config,
        )
        second = await graph.ainvoke(
            {"messages": [HumanMessage(content="second")]},
            session_config,
        )
        return second["rejected_capability_ids"]

    final_rejections = asyncio.run(exercise())

    assert rejections_seen_by_route == [[]]
    assert rejections_seen_by_capability == [[], []]
    assert final_rejections == []
