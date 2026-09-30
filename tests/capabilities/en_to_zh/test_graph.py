import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
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
        return "fake-en-to-zh-scope"

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
                                "name": "EnglishToChineseScopeDecision",
                                "args": {"can_translate_to_chinese": response},
                                "id": f"scope-call-{self.response_index}",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )


class FakeTranslationChatModel(BaseChatModel):
    responses: list[str]
    response_index: int = 0
    captured_messages: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fake-en-to-zh-translation"

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
            capability_id="en_to_zh",
            thread_id=str(config["configurable"]["thread_id"]),
            config=config,
        ),
        "task_input": TaskInput(messages=tuple(messages)),
    }


def _event_text(events: list[AgentTextEvent]) -> str:
    return "".join(event.text for event in events)


def test_en_to_zh_schema_and_graph_use_independent_state() -> None:
    from agent_runtime.capabilities.en_to_zh.graph import (
        EnglishToChineseScopeDecision,
        build_english_to_chinese_graph,
    )
    from agent_runtime.capabilities.en_to_zh.state import EnglishToChineseState
    from agent_runtime.graph.state import ParentState

    schema = EnglishToChineseScopeDecision.model_json_schema()
    graph = build_english_to_chinese_graph(
        model=FakeTranslationChatModel(responses=["译文"]),
        checkpointer=InMemorySaver(),
    )

    assert set(schema["properties"]) == {"can_translate_to_chinese"}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["can_translate_to_chinese"]["description"] == (
        "判断当前 HumanMessage 是否属于 Stage 1 允许的英文到中文翻译；true "
        "表示内容是纯英文待译文本或明确的英译汉请求，可以进入翻译图；false "
        "表示普通聊天、中文翻英文、其他语言互译、代码生成或其他非翻译任务，"
        "必须返回 OUT_OF_SCOPE。"
    )
    assert set(EnglishToChineseState.__annotations__) == {
        "messages",
        "draft_translation",
    }
    assert graph.builder.state_schema is EnglishToChineseState
    assert graph.builder.state_schema is not ParentState


def test_en_to_zh_translates_current_english_message_completely() -> None:
    from agent_runtime.capabilities.en_to_zh.graph import (
        EN_TO_ZH_SCOPE_PROMPT,
        EN_TO_ZH_SYSTEM_PROMPT,
        EnglishToChineseCapability,
    )
    from agent_runtime.graph.child_result import ChildResult

    scope_model = FakeScopeChatModel(responses=[True])
    translation_model = FakeTranslationChatModel(
        responses=["打开文件，保留每个标题，并且不要遗漏最后的警告。"]
    )
    saver = InMemorySaver()
    capability = EnglishToChineseCapability(
        scope_model=scope_model,
        translation_model=translation_model,
        checkpointer=saver,
    )
    config = child_thread_config(uuid4(), "en_to_zh")
    events: list[AgentTextEvent] = []

    result = asyncio.run(
        capability.run(
            **_contract_call(
                messages=[
                    HumanMessage(
                        content=(
                            "Open the file, preserve every heading, and do not omit "
                            "the final warning."
                        )
                    )
                ],
                config=config,
                events=events,
            )
        )
    )

    checkpoint = saver.get_tuple(config)
    assert checkpoint is not None
    assert result == ChildResult(status="completed", control_signal=None)
    assert _event_text(events) == (
        "打开文件，保留每个标题，并且不要遗漏最后的警告。"
    )
    assert checkpoint.checkpoint["channel_values"]["draft_translation"] == (
        "打开文件，保留每个标题，并且不要遗漏最后的警告。"
    )
    assert len(translation_model.captured_messages) == 1
    assert translation_model.captured_messages[0][0] == SystemMessage(
        content=EN_TO_ZH_SYSTEM_PROMPT
    )
    assert "只翻译当前 HumanMessage" in EN_TO_ZH_SYSTEM_PROMPT
    assert "不得省略、概括" in EN_TO_ZH_SYSTEM_PROMPT
    assert scope_model.captured_messages[0][0] == SystemMessage(
        content=EN_TO_ZH_SCOPE_PROMPT
    )


def test_en_to_zh_uses_history_only_for_translation_context() -> None:
    from agent_runtime.capabilities.en_to_zh.graph import (
        EN_TO_ZH_SYSTEM_PROMPT,
        EnglishToChineseCapability,
    )
    from agent_runtime.graph.child_result import ChildResult

    saver = InMemorySaver()
    translation_model = FakeTranslationChatModel(
        responses=["OpenAI 发布了它。", "它通过了所有检查。"]
    )
    capability = EnglishToChineseCapability(
        scope_model=FakeScopeChatModel(responses=[True, True]),
        translation_model=translation_model,
        checkpointer=saver,
    )
    config = child_thread_config(uuid4(), "en_to_zh")
    events: list[AgentTextEvent] = []

    first_result = asyncio.run(
        capability.run(
            **_contract_call(
                messages=[HumanMessage(content="OpenAI released it.")],
                config=config,
                events=events,
            )
        )
    )
    second_result = asyncio.run(
        capability.run(
            **_contract_call(
                messages=[HumanMessage(content="It passed all checks.")],
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
        "OpenAI released it.",
        "OpenAI 发布了它。",
        "It passed all checks.",
        "它通过了所有检查。",
    ]
    assert [message.content for message in translation_model.captured_messages[1]] == [
        EN_TO_ZH_SYSTEM_PROMPT,
        "OpenAI released it.",
        "OpenAI 发布了它。",
        "It passed all checks.",
    ]
    assert "历史消息仅用于理解代词、语境和术语" in EN_TO_ZH_SYSTEM_PROMPT


@pytest.mark.parametrize(
    "message",
    [
        "请介绍一下 FastAPI",
        "请把“你好”翻译成英文",
        "请把 bonjour 翻译成西班牙语",
        "请写一段 Python 代码",
    ],
)
def test_en_to_zh_rejects_requests_outside_english_to_chinese(message: str) -> None:
    from agent_runtime.capabilities.en_to_zh.graph import (
        EN_TO_ZH_SCOPE_PROMPT,
        EnglishToChineseCapability,
    )
    from agent_runtime.graph.child_result import ChildResult

    saver = InMemorySaver()
    translation_model = FakeTranslationChatModel(responses=["不应生成"])
    scope_model = FakeScopeChatModel(responses=[False])
    capability = EnglishToChineseCapability(
        scope_model=scope_model,
        translation_model=translation_model,
        checkpointer=saver,
    )
    config = child_thread_config(uuid4(), "en_to_zh")
    events: list[AgentTextEvent] = []

    result = asyncio.run(
        capability.run(
            **_contract_call(
                messages=[HumanMessage(content=message)],
                config=config,
                events=events,
            )
        )
    )

    assert result == ChildResult(
        status="rejected",
        control_signal="OUT_OF_SCOPE",
    )
    assert events == []
    assert saver.get_tuple(config) is None
    assert translation_model.captured_messages == []
    assert message in str(scope_model.captured_messages[0][-1].content)
    assert "中文翻英文" in EN_TO_ZH_SCOPE_PROMPT
    assert "其他语言互译" in EN_TO_ZH_SCOPE_PROMPT
    assert "代码生成" in EN_TO_ZH_SCOPE_PROMPT


def test_en_to_zh_rejection_does_not_advance_existing_child_history() -> None:
    from agent_runtime.capabilities.en_to_zh.graph import EnglishToChineseCapability
    from agent_runtime.graph.child_result import ChildResult

    saver = InMemorySaver()
    translation_model = FakeTranslationChatModel(responses=["早上好"])
    capability = EnglishToChineseCapability(
        scope_model=FakeScopeChatModel(responses=[True, False]),
        translation_model=translation_model,
        checkpointer=saver,
    )
    config = child_thread_config(uuid4(), "en_to_zh")
    accepted_events: list[AgentTextEvent] = []
    asyncio.run(
        capability.run(
            **_contract_call(
                messages=[HumanMessage(content="Good morning")],
                config=config,
                events=accepted_events,
            )
        )
    )
    checkpoint_before = saver.get_tuple(config)
    assert checkpoint_before is not None
    history_before = checkpoint_before.checkpoint["channel_values"].copy()
    rejected_events: list[AgentTextEvent] = []

    result = asyncio.run(
        capability.run(
            **_contract_call(
                messages=[HumanMessage(content="帮我介绍一下 FastAPI")],
                config=config,
                events=rejected_events,
            )
        )
    )

    checkpoint_after = saver.get_tuple(config)
    assert checkpoint_after is not None
    assert result == ChildResult(
        status="rejected",
        control_signal="OUT_OF_SCOPE",
    )
    assert rejected_events == []
    assert checkpoint_after.checkpoint["channel_values"] == history_before
    assert len(translation_model.captured_messages) == 1
