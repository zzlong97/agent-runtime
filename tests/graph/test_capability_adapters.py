import asyncio
from collections.abc import Iterator
from uuid import UUID, uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field


class FakeScopeChatModel(BaseChatModel):
    decision_name: str
    decision_field: str
    responses: list[bool]
    response_index: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake-capability-scope"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        response = self.responses[min(self.response_index, len(self.responses) - 1)]
        self.response_index += 1
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": self.decision_name,
                                "args": {self.decision_field: response},
                                "id": f"scope-{self.response_index}",
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
        return "fake-capability-response"

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


def test_adapters_refresh_child_views_and_keep_parent_authoritative() -> None:
    from agent_runtime.capabilities.en_to_zh.adapter import EnglishToChineseAdapter
    from agent_runtime.capabilities.en_to_zh.graph import (
        EnglishToChineseCapability,
    )
    from agent_runtime.capabilities.general_chat.adapter import GeneralChatAdapter
    from agent_runtime.capabilities.general_chat.agent import GeneralChatCapability
    from agent_runtime.graph.config import child_thread_config, parent_thread_config
    from agent_runtime.graph.parent import build_parent_graph

    session_id = uuid4()
    parent_config = parent_thread_config(session_id)
    parent_saver = InMemorySaver()
    general_saver = InMemorySaver()
    translation_saver = InMemorySaver()
    general_response_model = FakeResponseChatModel(responses=["我记住了。"])
    translation_model = FakeResponseChatModel(
        responses=["早上好。", "今天天气晴朗。"]
    )
    general_adapter = GeneralChatAdapter(
        capability=GeneralChatCapability(
            scope_model=FakeScopeChatModel(
                decision_name="GeneralChatScopeDecision",
                decision_field="is_translation_request",
                responses=[False, True],
            ),
            response_model=general_response_model,
            checkpointer=general_saver,
        ),
        message_id_factory=iter(
            [UUID("00000000-0000-0000-0000-000000000101")]
        ).__next__,
    )
    translation_adapter = EnglishToChineseAdapter(
        capability=EnglishToChineseCapability(
            scope_model=FakeScopeChatModel(
                decision_name="EnglishToChineseScopeDecision",
                decision_field="can_translate_to_chinese",
                responses=[True, True],
            ),
            translation_model=translation_model,
            checkpointer=translation_saver,
        ),
        message_id_factory=iter(
            [
                UUID("00000000-0000-0000-0000-000000000102"),
                UUID("00000000-0000-0000-0000-000000000103"),
            ]
        ).__next__,
    )
    route_rejections: list[list[str]] = []

    async def route(state, config):
        rejected = list(state["rejected_capability_ids"])
        route_rejections.append(rejected)
        capability_id = "en_to_zh" if "general_chat" in rejected else "general_chat"
        return {"resolved_capability_id": capability_id}

    async def invoke_capability(state, config):
        if state["resolved_capability_id"] == "general_chat":
            return await general_adapter.invoke(state, config)
        return await translation_adapter.invoke(state, config)

    parent = build_parent_graph(
        route=route,
        invoke_capability=invoke_capability,
        checkpointer=parent_saver,
    )
    first_human = HumanMessage(content="我喜欢蓝色", id=str(uuid4()))
    second_human = HumanMessage(content="Good morning.", id=str(uuid4()))
    third_human = HumanMessage(content="It is sunny today.", id=str(uuid4()))

    async def exercise() -> tuple[list[str], list[str], list[str]]:
        async for _event in parent.astream(
            {
                "messages": [first_human],
                "resolved_capability_id": None,
                "rejected_capability_ids": [],
            },
            parent_config,
            stream_mode="custom",
        ):
            pass
        async for _event in parent.astream(
            {"messages": [second_human]},
            parent_config,
            stream_mode="custom",
        ):
            pass
        async for _event in parent.astream(
            {"messages": [third_human]},
            parent_config,
            stream_mode="custom",
        ):
            pass

        parent_state = await parent.aget_state(parent_config)
        general_checkpoint = general_saver.get_tuple(
            child_thread_config(session_id, "general_chat")
        )
        translation_checkpoint = translation_saver.get_tuple(
            child_thread_config(session_id, "en_to_zh")
        )
        assert general_checkpoint is not None
        assert translation_checkpoint is not None
        return (
            [message.content for message in parent_state.values["messages"]],
            [
                message.content
                for message in general_checkpoint.checkpoint["channel_values"][
                    "messages"
                ]
            ],
            [
                message.content
                for message in translation_checkpoint.checkpoint["channel_values"][
                    "messages"
                ]
            ],
        )

    parent_messages, general_messages, translation_messages = asyncio.run(exercise())

    assert route_rejections == [[], ["general_chat"]]
    assert parent_messages == [
        "我喜欢蓝色",
        "我记住了。",
        "Good morning.",
        "早上好。",
        "It is sunny today.",
        "今天天气晴朗。",
    ]
    assert general_messages == ["我喜欢蓝色", "我记住了。"]
    assert translation_messages == parent_messages
    assert [
        message.content for message in translation_model.captured_messages[-1][1:]
    ] == parent_messages[:-1]
    assert parent_messages.count("我喜欢蓝色") == 1
    assert general_saver.get_tuple(child_thread_config(session_id, "en_to_zh")) is None
    assert (
        translation_saver.get_tuple(
            child_thread_config(session_id, "general_chat")
        )
        is None
    )
