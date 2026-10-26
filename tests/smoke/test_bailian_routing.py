"""显式启用的真实模型路由与范围守卫验证，不连接业务数据库。"""

import asyncio
import os
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent_runtime.capabilities.en_to_zh.graph import EnglishToChineseCapability
from agent_runtime.capabilities.general_chat.agent import GeneralChatCapability
from agent_runtime.core.config import Settings
from agent_runtime.core.model import build_chat_model
from agent_runtime.graph.router import StageOneRouter
from agent_runtime.runtime.agent_contract import (
    AgentCancellation,
    AgentContext,
    AgentEventOutlet,
    RunContext,
    TaskInput,
)
from tests.graph.test_router import FakeCandidateProvider

pytestmark = [
    pytest.mark.bailian,
    pytest.mark.skipif(
        os.getenv("RUN_BAILIAN_SMOKE") != "1",
        reason="设置 RUN_BAILIAN_SMOKE=1 后验证真实模型路由和守卫",
    ),
]


@pytest.mark.parametrize(("capability_id", "content"), [
    ("general_chat", "你好，请简短介绍一下你自己。"),
    ("en_to_zh", "请把英文 Hello 翻译为中文。"),
])
def test_bailian_router_guard_and_generation(capability_id, content):
    """仅发送合成输入，验证 Router、守卫和可见生成的真实 SDK 链路。"""

    async def never_cancelled():
        return False

    async def scenario():
        model = build_chat_model(Settings())
        model.max_retries = 0
        router = StageOneRouter(
            model, candidate_provider=FakeCandidateProvider(), user_id="runtime-user",
        )
        message = HumanMessage(content=content)
        decision = await router.route({
            "messages": [message], "resolved_capability_id": None,
            "rejected_capability_ids": [],
        }, {})
        assert decision["resolved_capability_id"] == capability_id
        saver, session_id, events = InMemorySaver(), uuid4(), []
        if capability_id == "general_chat":
            capability = GeneralChatCapability(
                scope_model=model, response_model=model, checkpointer=saver,
            )
        else:
            capability = EnglishToChineseCapability(
                scope_model=model, translation_model=model, checkpointer=saver,
            )
        config = {"configurable": {"thread_id": f"{session_id}:{capability_id}"}}
        result = await capability.invoke(
            run_context=RunContext(
                run_id=uuid4(), request_id=uuid4(), input_message_id=uuid4(),
                response_message_id=uuid4(),
                cancellation=AgentCancellation(never_cancelled),
                events=AgentEventOutlet(events.append),
            ),
            agent_context=AgentContext(
                session_id=session_id, capability_id=capability_id,
                thread_id=config["configurable"]["thread_id"], config=config,
            ),
            task_input=TaskInput(messages=(message,)),
        )
        assert result.status == "completed"
        assert result.content.strip()
        assert "".join(event.text for event in events) == result.content
    asyncio.run(asyncio.wait_for(scenario(), timeout=60))
