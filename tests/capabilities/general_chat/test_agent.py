import asyncio
from typing import Any
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from agent_runtime.graph.config import child_thread_config


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


def _event_text(events: list[tuple[BaseMessage, dict[str, Any]]]) -> str:
    return "".join(
        str(message.content)
        for message, _metadata in events
        if isinstance(message, AIMessage) and message.content
    )


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

    scope_model = FakeScopeChatModel(responses=[False])
    response_model = FakeResponseChatModel(
        responses=["LangGraph 是用于构建有状态 Agent 工作流的框架。"]
    )
    capability = GeneralChatCapability(
        scope_model=scope_model,
        response_model=response_model,
        checkpointer=InMemorySaver(),
    )
    events: list[tuple[BaseMessage, dict[str, Any]]] = []

    result = asyncio.run(
        capability.run(
            messages=[HumanMessage(content="请简要介绍 LangGraph")],
            config=child_thread_config(uuid4(), "general_chat"),
            emit=events.append,
        )
    )

    assert result == "completed"
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

    first_result = asyncio.run(
        capability.run(
            messages=[HumanMessage(content="我喜欢蓝色")],
            config=config,
            emit=lambda _event: None,
        )
    )
    second_result = asyncio.run(
        capability.run(
            messages=[HumanMessage(content="我刚才说喜欢什么颜色？")],
            config=config,
            emit=lambda _event: None,
        )
    )

    checkpoint = saver.get_tuple(config)
    assert checkpoint is not None
    messages = checkpoint.checkpoint["channel_values"]["messages"]
    assert first_result == "completed"
    assert second_result == "completed"
    assert [message.content for message in messages] == [
        "我喜欢蓝色",
        "我记住了。",
        "我刚才说喜欢什么颜色？",
        "你刚才说你喜欢蓝色。",
    ]
    assert len(response_model.captured_messages) == 2


def test_translation_rejection_emits_nothing_and_does_not_advance_history() -> None:
    from agent_runtime.capabilities.general_chat.agent import GeneralChatCapability

    saver = InMemorySaver()
    response_model = FakeResponseChatModel(responses=["普通聊天回复"])
    capability = GeneralChatCapability(
        scope_model=FakeScopeChatModel(responses=[False, True]),
        response_model=response_model,
        checkpointer=saver,
    )
    config = child_thread_config(uuid4(), "general_chat")
    asyncio.run(
        capability.run(
            messages=[HumanMessage(content="你好")],
            config=config,
            emit=lambda _event: None,
        )
    )
    checkpoint_before = saver.get_tuple(config)
    assert checkpoint_before is not None
    history_before = list(
        checkpoint_before.checkpoint["channel_values"]["messages"]
    )
    rejected_events: list[tuple[BaseMessage, dict[str, Any]]] = []

    result = asyncio.run(
        capability.run(
            messages=[HumanMessage(content="请把 hello 翻译成中文")],
            config=config,
            emit=rejected_events.append,
        )
    )

    checkpoint_after = saver.get_tuple(config)
    assert checkpoint_after is not None
    history_after = checkpoint_after.checkpoint["channel_values"]["messages"]
    assert result == "OUT_OF_SCOPE"
    assert rejected_events == []
    assert history_after == history_before
    assert len(response_model.captured_messages) == 1
