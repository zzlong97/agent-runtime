"""Parent Graph 对开放 Capability ID 的动态有限回流测试。"""

import asyncio
from uuid import uuid4

from langchain_core.messages import HumanMessage

from agent_runtime.graph.child_result import CapabilityInvocation, ChildResult
from agent_runtime.graph.config import parent_thread_config
from agent_runtime.graph.parent import UNSUPPORTED_REPLY, build_parent_graph


def test_parent_dispatches_dynamic_ids_without_fixed_capability_collection() -> None:
    """Parent 应把动态 ID 交给 Gateway，并由 Router 决定何时候选耗尽。"""

    routed: list[tuple[str, ...]] = []
    invoked: list[str] = []

    async def route(state, _config):
        rejected = tuple(state["rejected_capability_ids"])
        routed.append(rejected)
        if not rejected:
            return {
                "resolved_capability_id": "dynamic_alpha",
                "task_action": "new",
            }
        if rejected == ("dynamic_alpha",):
            return {
                "resolved_capability_id": "dynamic_beta",
                "task_action": "continue",
            }
        return {"resolved_capability_id": None, "task_action": None}

    async def invoke(state, _config):
        invoked.append(state["resolved_capability_id"])
        return CapabilityInvocation(
            result=ChildResult(
                status="rejected",
                control_signal="OUT_OF_SCOPE",
            )
        )

    graph = build_parent_graph(route=route, invoke_capability=invoke)
    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="动态能力请求")],
                "resolved_capability_id": None,
                "task_action": None,
                "rejected_capability_ids": [],
                "completion_status": None,
            },
            parent_thread_config(uuid4(), message_id=uuid4()),
        )
    )

    assert invoked == ["dynamic_alpha", "dynamic_beta"]
    assert routed == [(), ("dynamic_alpha",), ("dynamic_alpha", "dynamic_beta")]
    assert result["completion_status"] == "unsupported"
    assert result["messages"][-1].text == UNSUPPORTED_REPLY
