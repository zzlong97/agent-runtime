import asyncio
from typing import Any
from uuid import UUID, uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from agent_runtime.graph.config import child_thread_config
from agent_runtime.runtime.agent_contract import (
    AgentCancellation,
    AgentContext,
    AgentEventOutlet,
    AgentTextEvent,
    RunContext,
    TaskInput,
)


class FakeScopeChatModel(BaseChatModel):
    responses: list[bool]
    response_index: int = 0
    captured_messages: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fake-general-chat-scope"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        self.captured_messages.append(messages)
        response = self.responses[min(self.response_index, len(self.responses) - 1)]
        self.response_index += 1
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "GeneralChatScopeDecision",
                                "args": {"is_translation_request": response},
                                "id": f"scope-call-{self.response_index}",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )


class FakeResponseChatModel(BaseChatModel):
    responses: list[str]
    response_index: int = 0
    captured_messages: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fake-general-chat-response"

    def _generate(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        self.captured_messages.append(messages)
        response = self.responses[min(self.response_index, len(self.responses) - 1)]
        self.response_index += 1
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=response))]
        )


async def _never_cancelled() -> bool:
    return False


def _contract_call(
    *,
    messages: list[BaseMessage],
    config: dict[str, Any],
    events: list[AgentTextEvent],
) -> dict[str, object]:
    """为能力单测构造不含基础设施对象的统一 Agent Contract。"""

    session_id = UUID(str(config["configurable"]["thread_id"]).split(":")[0])
    return {
        "run_context": RunContext(
            run_id=uuid4(),
            request_id=uuid4(),
            input_message_id=uuid4(),
            response_message_id=uuid4(),
            cancellation=AgentCancellation(_never_cancelled),
            events=AgentEventOutlet(events.append),
        ),
        "agent_context": AgentContext(
            session_id=session_id,
            capability_id="general_chat",
            thread_id=str(config["configurable"]["thread_id"]),
            config=config,
        ),
        "task_input": TaskInput(messages=tuple(messages)),
    }


def _event_text(events: list[AgentTextEvent]) -> str:
    return "".join(event.text for event in events)


def test_general_chat_scope_schema_describes_translation_boundary() -> None:
    from agent_runtime.capabilities.general_chat.agent import (
        GeneralChatScopeDecision,
    )

    schema = GeneralChatScopeDecision.model_json_schema()

    assert set(schema["properties"]) == {"is_translation_request"}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["is_translation_request"]["description"] == (
        "判断当前 HumanMessage 是否要求执行任何语言方向的翻译；true 表示 "
        "general_chat 必须返回 OUT_OF_SCOPE，false 表示可进入普通聊天生成。"
    )


def test_general_chat_answers_ordinary_chat_with_system_prompt() -> None:
    from agent_runtime.capabilities.general_chat.agent import (
        GENERAL_CHAT_SCOPE_PROMPT,
        GENERAL_CHAT_SYSTEM_PROMPT,
        GeneralChatCapability,
    )
    from agent_runtime.graph.child_result import ChildResult

    scope_model = FakeScopeChatModel(responses=[False])
    response_model = FakeResponseChatModel(
        responses=["LangGraph 是用于构建有状态 Agent 工作流的框架。"]
    )
    capability = GeneralChatCapability(
        scope_model=scope_model,
        response_model=response_model,
        checkpointer=InMemorySaver(),
    )
    events: list[AgentTextEvent] = []
    config = child_thread_config(uuid4(), "general_chat")

    result = asyncio.run(
        capability.run(
            **_contract_call(
                messages=[HumanMessage(content="请简要介绍 LangGraph")],
                config=config,
                events=events,
            )
        )
    )

    assert result == ChildResult(status="completed", control_signal=None)
    assert _event_text(events) == "LangGraph 是用于构建有状态 Agent 工作流的框架。"
    assert len(response_model.captured_messages) == 1
    assert response_model.captured_messages[0][0] == SystemMessage(
        content=GENERAL_CHAT_SYSTEM_PROMPT
    )
    assert "简洁、直接" in GENERAL_CHAT_SYSTEM_PROMPT
    assert "不得执行任何翻译" in GENERAL_CHAT_SYSTEM_PROMPT
    assert scope_model.captured_messages[0][0] == SystemMessage(
        content=GENERAL_CHAT_SCOPE_PROMPT
    )


def test_general_chat_preserves_multi_turn_child_history() -> None:
    from agent_runtime.capabilities.general_chat.agent import GeneralChatCapability
    from agent_runtime.graph.child_result import ChildResult

    saver = InMemorySaver()
    response_model = FakeResponseChatModel(
        responses=["我记住了。", "你刚才说你喜欢蓝色。"]
    )
    capability = GeneralChatCapability(
        scope_model=FakeScopeChatModel(responses=[False, False]),
        response_model=response_model,
        checkpointer=saver,
    )
    config = child_thread_config(uuid4(), "general_chat")
    events: list[AgentTextEvent] = []

    first_result = asyncio.run(
        capability.run(
            **_contract_call(
                messages=[HumanMessage(content="我喜欢蓝色")],
                config=config,
                events=events,
            )
        )
    )
    second_result = asyncio.run(
        capability.run(
            **_contract_call(
                messages=[HumanMessage(content="我刚才说喜欢什么颜色？")],
                config=config,
                events=events,
            )
        )
    )

    checkpoint = saver.get_tuple(config)
    assert checkpoint is not None
    messages = checkpoint.checkpoint["channel_values"]["messages"]
    assert first_result == ChildResult(status="completed", control_signal=None)
    assert second_result == ChildResult(status="completed", control_signal=None)
    assert [message.content for message in messages] == [
        "我喜欢蓝色",
        "我记住了。",
        "我刚才说喜欢什么颜色？",
        "你刚才说你喜欢蓝色。",
    ]
    assert len(response_model.captured_messages) == 2


def test_translation_rejection_emits_nothing_and_does_not_advance_history() -> None:
    from agent_runtime.capabilities.general_chat.agent import GeneralChatCapability
    from agent_runtime.graph.child_result import ChildResult

    saver = InMemorySaver()
    response_model = FakeResponseChatModel(responses=["普通聊天回复"])
    capability = GeneralChatCapability(
        scope_model=FakeScopeChatModel(responses=[False, True]),
        response_model=response_model,
        checkpointer=saver,
    )
    config = child_thread_config(uuid4(), "general_chat")
    accepted_events: list[AgentTextEvent] = []
    asyncio.run(
        capability.run(
            **_contract_call(
                messages=[HumanMessage(content="你好")],
                config=config,
                events=accepted_events,
            )
        )
    )
    checkpoint_before = saver.get_tuple(config)
    assert checkpoint_before is not None
    history_before = list(
        checkpoint_before.checkpoint["channel_values"]["messages"]
    )
    rejected_events: list[AgentTextEvent] = []

    result = asyncio.run(
        capability.run(
            **_contract_call(
                messages=[HumanMessage(content="请把 hello 翻译成中文")],
                config=config,
                events=rejected_events,
            )
        )
    )

    checkpoint_after = saver.get_tuple(config)
    assert checkpoint_after is not None
    history_after = checkpoint_after.checkpoint["channel_values"]["messages"]
    assert result == ChildResult(
        status="rejected",
        control_signal="OUT_OF_SCOPE",
    )
    assert rejected_events == []
    assert history_after == history_before
    assert len(response_model.captured_messages) == 1
