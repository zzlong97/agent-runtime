import asyncio
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver


def test_parent_graph_runs_the_fixed_stage_one_skeleton() -> None:
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.parent import build_parent_graph

    calls: list[str] = []

    async def route(state, config):
        calls.append("route")
        return {"resolved_capability_id": "general_chat"}

    async def invoke_capability(state, config):
        calls.append("invoke_capability")
        assert state["resolved_capability_id"] == "general_chat"
        return {}

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
        "route",
        "invoke_capability",
        "__end__",
    }


def test_parent_graph_resets_rejections_before_each_new_request() -> None:
    from agent_runtime.graph.config import parent_thread_config
    from agent_runtime.graph.parent import build_parent_graph

    rejections_seen_by_route: list[list[str]] = []

    async def route(state, config):
        rejections_seen_by_route.append(state["rejected_capability_ids"])
        return {"resolved_capability_id": "general_chat"}

    async def invoke_capability(state, config):
        return {}

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

    assert rejections_seen_by_route == [[], []]
    assert final_rejections == []
