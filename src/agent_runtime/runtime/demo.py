"""开发环境确定性 Runtime 演示图。"""

import asyncio
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Checkpointer, interrupt

from agent_runtime.core.errors import ApplicationError
from agent_runtime.core.logging import log_business_event
from agent_runtime.graph.config import public_message_id_from_parent_config
from agent_runtime.graph.state import ParentState

import logging


logger = logging.getLogger(__name__)

_NORMAL_COMMAND = "/demo normal"
_INTERRUPT_COMMAND = "/demo interrupt"
_FAIL_COMMAND = "/demo fail"
_SLOW_COMMAND = "/demo slow"
_ALLOWED_COMMANDS = frozenset(
    {_NORMAL_COMMAND, _INTERRUPT_COMMAND, _FAIL_COMMAND, _SLOW_COMMAND}
)


class RuntimeDemoError(ApplicationError):
    """演示场景收到非法命令或按预期触发失败。"""


def _current_command(state: ParentState) -> str:
    """只读取当前公共历史中最新一条 HumanMessage。"""

    current = next(
        (
            message
            for message in reversed(state["messages"])
            if isinstance(message, HumanMessage)
        ),
        None,
    )
    if current is None:
        raise RuntimeDemoError(
            code="RUNTIME_DEMO_INPUT_MISSING",
            message="演示运行缺少用户输入。",
            retryable=False,
        )
    command = str(current.content).strip().casefold()
    if command not in _ALLOWED_COMMANDS:
        raise RuntimeDemoError(
            code="RUNTIME_DEMO_SCENARIO_INVALID",
            message=(
                "演示模式只接受 /demo normal、/demo interrupt、"
                "/demo fail 或 /demo slow。"
            ),
            status_code=409,
            retryable=False,
        )
    return command


async def _write_chunks(chunks: tuple[str, ...], *, delay: float = 0) -> str:
    """通过 LangGraph custom stream 写出确定性公开文本片段。"""

    writer = get_stream_writer()
    for index, chunk in enumerate(chunks):
        writer(AIMessageChunk(content=chunk))
        if delay and index < len(chunks) - 1:
            await asyncio.sleep(delay)
    return "".join(chunks)


def build_runtime_demo_graph(*, checkpointer: Checkpointer = None):
    """构建绕过正式 Router、但使用真实 Checkpoint 的开发演示图。"""

    async def execute_demo(
        state: ParentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        command = _current_command(state)
        log_business_event(
            logger,
            "Runtime演示场景开始",
            session_id=config.get("configurable", {}).get("thread_id"),
            scenario=command.removeprefix("/demo "),
        )
        if command == _FAIL_COMMAND:
            raise RuntimeDemoError(
                code="RUNTIME_DEMO_FAILURE",
                message="演示运行按预期失败。",
                retryable=False,
            )
        if command == _INTERRUPT_COMMAND:
            resume_payload = interrupt(
                {
                    "prompt": "是否继续完成演示运行？",
                }
            )
            approved = bool(
                isinstance(resume_payload, dict)
                and resume_payload.get("approved") is True
            )
            content = await _write_chunks(
                (
                    "演示运行已在确认后继续完成。"
                    if approved
                    else "演示运行已按拒绝结果结束。",
                )
            )
        elif command == _SLOW_COMMAND:
            content = await _write_chunks(
                ("演示慢速输出：", "第一段，", "第二段，", "已完成。"),
                delay=0.5,
            )
        else:
            content = await _write_chunks(("演示运行已", "正常完成。"))

        message_id = public_message_id_from_parent_config(config)
        if message_id is None:
            raise RuntimeDemoError(
                code="RUNTIME_DEMO_MESSAGE_ID_MISSING",
                message="演示运行缺少稳定消息标识。",
                retryable=True,
            )
        message = AIMessage(
            content=content,
            id=str(message_id),
            additional_kwargs={
                "runtime_status": "completed",
                "capability_id": None,
            },
        )
        log_business_event(
            logger,
            "Runtime演示场景完成",
            session_id=config.get("configurable", {}).get("thread_id"),
            message_id=message_id,
            scenario=command.removeprefix("/demo "),
            status="completed",
        )
        return {
            "messages": [message],
            "resolved_capability_id": None,
            "rejected_capability_ids": [],
            "completion_status": "completed",
        }

    builder = StateGraph(ParentState)
    # 复用 ChatService 已确认的最终公共消息写入节点名；该单节点图不包含
    # route 节点，也不会构造或调用正式 Capability Router。
    builder.add_node("invoke_capability", execute_demo)
    builder.add_edge(START, "invoke_capability")
    builder.add_edge("invoke_capability", END)
    return builder.compile(checkpointer=checkpointer)
