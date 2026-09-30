import asyncio
from uuid import UUID

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command


SESSION_ID = UUID("00000000-0000-0000-0000-000000002590")
MESSAGE_ID = UUID("00000000-0000-0000-0000-000000002591")


def _config() -> dict:
    return {
        "configurable": {
            "thread_id": str(SESSION_ID),
            "message_id": str(MESSAGE_ID),
        }
    }


def test_demo_graph_completes_without_formal_capability_router() -> None:
    from agent_runtime.runtime.demo import build_runtime_demo_graph

    async def exercise():
        graph = build_runtime_demo_graph(checkpointer=InMemorySaver())
        events = [
            event
            async for event in graph.astream(
                {"messages": [HumanMessage(content="/demo normal")]},
                _config(),
                stream_mode="custom",
            )
        ]
        snapshot = await graph.aget_state(_config())
        return events, snapshot

    events, snapshot = asyncio.run(exercise())

    assert "".join(str(event.content) for event in events) == "演示运行已正常完成。"
    assert snapshot.values["completion_status"] == "completed"
    assert snapshot.values["resolved_capability_id"] is None
    assert snapshot.values["messages"][-1].id == str(MESSAGE_ID)
    assert snapshot.values["messages"][-1].additional_kwargs == {
        "runtime_status": "completed",
        "capability_id": None,
    }


def test_demo_graph_interrupts_and_resumes_same_checkpoint() -> None:
    from agent_runtime.runtime.demo import build_runtime_demo_graph

    async def exercise():
        graph = build_runtime_demo_graph(checkpointer=InMemorySaver())
        first_events = [
            event
            async for event in graph.astream(
                {"messages": [HumanMessage(content="/demo interrupt")]},
                _config(),
                stream_mode="custom",
            )
        ]
        interrupted = await graph.aget_state(_config())
        resumed_events = [
            event
            async for event in graph.astream(
                Command(resume={"approved": True}),
                _config(),
                stream_mode="custom",
            )
        ]
        completed = await graph.aget_state(_config())
        return first_events, interrupted, resumed_events, completed

    first_events, interrupted, resumed_events, completed = asyncio.run(exercise())

    assert first_events == []
    assert len(interrupted.interrupts) == 1
    assert interrupted.interrupts[0].value == {
        "prompt": "是否继续完成演示运行？",
    }
    assert "".join(str(event.content) for event in resumed_events) == (
        "演示运行已在确认后继续完成。"
    )
    assert completed.interrupts == ()
    assert completed.values["messages"][-1].id == str(MESSAGE_ID)


def test_demo_graph_exposes_deterministic_failure() -> None:
    from agent_runtime.runtime.demo import (
        RuntimeDemoError,
        build_runtime_demo_graph,
    )

    async def exercise():
        graph = build_runtime_demo_graph(checkpointer=InMemorySaver())
        async for _event in graph.astream(
            {"messages": [HumanMessage(content="/demo fail")]},
            _config(),
            stream_mode="custom",
        ):
            pass

    with pytest.raises(RuntimeDemoError) as caught:
        asyncio.run(exercise())

    assert caught.value.code == "RUNTIME_DEMO_FAILURE"
    assert caught.value.message == "演示运行按预期失败。"
