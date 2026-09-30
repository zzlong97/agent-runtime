"""S2.5-09 最小 Agent 执行契约测试。"""

import asyncio
from dataclasses import fields
from typing import TypedDict
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


def _build_contexts(*, cancellation_probe=None):
    from agent_runtime.graph.config import child_thread_config
    from agent_runtime.runtime.agent_contract import (
        AgentCancellation,
        AgentContext,
        AgentEventOutlet,
        RunContext,
        TaskInput,
    )

    session_id = uuid4()
    emitted = []

    async def never_cancelled() -> bool:
        return False

    run_context = RunContext(
        run_id=uuid4(),
        request_id=uuid4(),
        input_message_id=uuid4(),
        response_message_id=uuid4(),
        cancellation=AgentCancellation(
            cancellation_probe or never_cancelled
        ),
        events=AgentEventOutlet(emitted.append),
    )
    agent_context = AgentContext(
        session_id=session_id,
        capability_id="general_chat",
        thread_id=f"{session_id}:general_chat",
        config=child_thread_config(session_id, "general_chat"),
    )
    task_input = TaskInput(
        messages=(HumanMessage(content="测试输入", id=str(uuid4())),)
    )
    return run_context, agent_context, task_input, emitted


def test_agent_contract_exposes_only_controlled_execution_inputs() -> None:
    """Agent 上下文不得携带 SSE、Redis、Session 删除或 Run Repository。"""

    from agent_runtime.runtime.agent_contract import (
        AgentContext,
        RunContext,
        TaskInput,
    )

    assert {field.name for field in fields(RunContext)} == {
        "run_id",
        "request_id",
        "input_message_id",
        "response_message_id",
        "cancellation",
        "events",
    }
    assert {field.name for field in fields(AgentContext)} == {
        "session_id",
        "capability_id",
        "thread_id",
        "config",
    }
    assert {field.name for field in fields(TaskInput)} == {"messages"}


def test_agent_event_outlet_accepts_only_typed_nonempty_text() -> None:
    """事件出口只接受已定义文本事件，拒绝原始消息和私有元数据。"""

    from langchain_core.messages import AIMessage
    from pydantic import ValidationError

    from agent_runtime.runtime.agent_contract import (
        AgentEventOutlet,
        AgentTextEvent,
    )

    emitted = []
    outlet = AgentEventOutlet(emitted.append)
    event = AgentTextEvent(text="公开文本")

    outlet.emit(event)

    assert emitted == [event]
    assert AgentTextEvent.model_json_schema()["properties"]["text"][
        "description"
    ] == (
        "Agent 本次生成的非空用户可见文本片段；不得包含 Prompt、私有 State、"
        "工具原始参数、工具原始结果或基础设施对象。"
    )
    with pytest.raises(TypeError, match="类型化事件"):
        outlet.emit(AIMessage(content="绕过契约"))
    with pytest.raises(ValidationError):
        AgentTextEvent(text="")
    with pytest.raises(ValidationError):
        AgentTextEvent(text="公开文本", private_state={"secret": "不得外泄"})


def test_fake_slow_agent_observes_injected_cancellation() -> None:
    """慢速 Agent 只能通过注入的取消探针感知取消并传播标准取消异常。"""

    from agent_runtime.runtime.agent_contract import execute_agent

    probe_calls = 0

    async def cancellation_probe() -> bool:
        nonlocal probe_calls
        probe_calls += 1
        return probe_calls >= 2

    run_context, agent_context, task_input, _emitted = _build_contexts(
        cancellation_probe=cancellation_probe
    )

    class FakeSlowAgent:
        async def run(self, *, run_context, agent_context, task_input):
            while True:
                await run_context.cancellation.raise_if_requested()
                await asyncio.sleep(0)

    async def exercise() -> None:
        with pytest.raises(asyncio.CancelledError):
            await execute_agent(
                FakeSlowAgent(),
                run_context=run_context,
                agent_context=agent_context,
                task_input=task_input,
            )

    asyncio.run(exercise())
    assert probe_calls == 2


def test_fake_failed_agent_keeps_child_result_as_thin_control_plane() -> None:
    """失败 Agent 仍只返回 ChildResult，不得把正文复制到控制面。"""

    from agent_runtime.graph.child_result import ChildResult
    from agent_runtime.runtime.agent_contract import execute_agent

    run_context, agent_context, task_input, emitted = _build_contexts()

    class FakeFailedAgent:
        async def run(self, *, run_context, agent_context, task_input):
            return ChildResult(status="failed", control_signal=None)

    result = asyncio.run(
        execute_agent(
            FakeFailedAgent(),
            run_context=run_context,
            agent_context=agent_context,
            task_input=task_input,
        )
    )

    assert result == ChildResult(status="failed", control_signal=None)
    assert set(result.model_dump()) == {"status", "control_signal"}
    assert emitted == []


def test_fake_interrupt_agent_uses_same_contract_and_langgraph_checkpoint() -> None:
    """中断 Agent 沿用同一契约，并由 LangGraph Checkpoint 承载恢复输入。"""

    from agent_runtime.graph.child_result import ChildResult
    from agent_runtime.runtime.agent_contract import AgentTextEvent, execute_agent

    run_context, agent_context, task_input, emitted = _build_contexts()

    class InterruptState(TypedDict, total=False):
        status: str

    class FakeInterruptAgent:
        async def run(self, *, run_context, agent_context, task_input):
            resume_payload = interrupt({"prompt": "是否继续？"})
            assert resume_payload == {"approved": True}
            run_context.events.emit(AgentTextEvent(text="已恢复执行。"))
            return ChildResult(status="completed", control_signal=None)

    fake_agent = FakeInterruptAgent()

    async def invoke_fake_agent(_state):
        result = await execute_agent(
            fake_agent,
            run_context=run_context,
            agent_context=agent_context,
            task_input=task_input,
        )
        return {"status": result.status}

    builder = StateGraph(InterruptState)
    builder.add_node("invoke_agent", invoke_fake_agent)
    builder.add_edge(START, "invoke_agent")
    builder.add_edge("invoke_agent", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}

    async def exercise() -> tuple[object, object]:
        await graph.ainvoke({}, config)
        interrupted = await graph.aget_state(config)
        resumed = await graph.ainvoke(
            Command(resume={"approved": True}),
            config,
        )
        return interrupted, resumed

    interrupted, resumed = asyncio.run(exercise())

    assert len(interrupted.interrupts) == 1
    assert interrupted.interrupts[0].value == {"prompt": "是否继续？"}
    assert resumed["status"] == "completed"
    assert [event.text for event in emitted] == ["已恢复执行。"]
