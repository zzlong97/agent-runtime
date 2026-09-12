import asyncio
import os
from uuid import UUID, uuid4

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


class FakeScopeChatModel(BaseChatModel):
    decision_name: str
    decision_field: str
    responses: list[bool]
    response_index: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake-postgres-scope"

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
        return "fake-postgres-response"

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


def run_on_psycopg_compatible_loop(coroutine):
    from agent_runtime.core.event_loop import psycopg_compatible_loop_factory

    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coroutine)


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
)
def test_stage_one_runtime_restores_parent_and_isolates_child_checkpoints() -> None:
    from agent_runtime.capabilities.en_to_zh.adapter import EnglishToChineseAdapter
    from agent_runtime.capabilities.en_to_zh.graph import (
        EnglishToChineseCapability,
    )
    from agent_runtime.capabilities.general_chat.adapter import GeneralChatAdapter
    from agent_runtime.capabilities.general_chat.agent import GeneralChatCapability
    from agent_runtime.core.config import Settings
    from agent_runtime.graph.config import child_thread_config, parent_thread_config
    from agent_runtime.graph.parent import build_parent_graph
    from agent_runtime.persistence.checkpointers import open_stage_one_checkpointers

    settings = Settings()
    session_id = uuid4()
    parent_config = parent_thread_config(session_id)
    first_human = HumanMessage(content="我喜欢蓝色", id=str(uuid4()))
    second_human = HumanMessage(content="Good morning.", id=str(uuid4()))
    third_human = HumanMessage(content="It is sunny today.", id=str(uuid4()))
    route_rejections: list[list[str]] = []
    restarted_translation_model = FakeResponseChatModel(
        responses=["早上好。", "今天天气晴朗。"]
    )

    async def exercise() -> None:
        try:
            async with open_stage_one_checkpointers(settings) as first_savers:
                general_adapter = GeneralChatAdapter(
                    capability=GeneralChatCapability(
                        scope_model=FakeScopeChatModel(
                            decision_name="GeneralChatScopeDecision",
                            decision_field="is_translation_request",
                            responses=[False],
                        ),
                        response_model=FakeResponseChatModel(responses=["我记住了。"]),
                        checkpointer=first_savers.general_chat,
                    ),
                    message_id_factory=lambda: UUID(
                        "00000000-0000-0000-0000-000000000201"
                    ),
                )

                async def first_route(state, config):
                    return {"resolved_capability_id": "general_chat"}

                first_parent = build_parent_graph(
                    route=first_route,
                    invoke_capability=general_adapter.invoke,
                    checkpointer=first_savers.parent,
                )
                async for _event in first_parent.astream(
                    {
                        "messages": [first_human],
                        "resolved_capability_id": None,
                        "rejected_capability_ids": [],
                    },
                    parent_config,
                    stream_mode="custom",
                ):
                    pass

            async with open_stage_one_checkpointers(settings) as restarted_savers:
                general_adapter = GeneralChatAdapter(
                    capability=GeneralChatCapability(
                        scope_model=FakeScopeChatModel(
                            decision_name="GeneralChatScopeDecision",
                            decision_field="is_translation_request",
                            responses=[True],
                        ),
                        response_model=FakeResponseChatModel(
                            responses=["不应生成普通聊天回复"]
                        ),
                        checkpointer=restarted_savers.general_chat,
                    )
                )
                translation_message_ids = iter(
                    [
                        UUID("00000000-0000-0000-0000-000000000202"),
                        UUID("00000000-0000-0000-0000-000000000203"),
                    ]
                )
                translation_adapter = EnglishToChineseAdapter(
                    capability=EnglishToChineseCapability(
                        scope_model=FakeScopeChatModel(
                            decision_name="EnglishToChineseScopeDecision",
                            decision_field="can_translate_to_chinese",
                            responses=[True, True],
                        ),
                        translation_model=restarted_translation_model,
                        checkpointer=restarted_savers.en_to_zh,
                    ),
                    message_id_factory=translation_message_ids.__next__,
                )

                async def restarted_route(state, config):
                    rejected = list(state["rejected_capability_ids"])
                    route_rejections.append(rejected)
                    return {"resolved_capability_id": "en_to_zh"}

                async def invoke_capability(state, config):
                    if state["resolved_capability_id"] == "general_chat":
                        return await general_adapter.invoke(state, config)
                    return await translation_adapter.invoke(state, config)

                restarted_parent = build_parent_graph(
                    route=restarted_route,
                    invoke_capability=invoke_capability,
                    checkpointer=restarted_savers.parent,
                )
                async for _event in restarted_parent.astream(
                    {"messages": [second_human]},
                    parent_config,
                    stream_mode="custom",
                ):
                    pass
                async for _event in restarted_parent.astream(
                    {"messages": [third_human]},
                    parent_config,
                    stream_mode="custom",
                ):
                    pass

                restored_parent = await restarted_parent.aget_state(parent_config)
                general_checkpoint = await restarted_savers.general_chat.aget_tuple(
                    child_thread_config(session_id, "general_chat")
                )
                translation_checkpoint = await restarted_savers.en_to_zh.aget_tuple(
                    child_thread_config(session_id, "en_to_zh")
                )
                assert general_checkpoint is not None
                assert translation_checkpoint is not None
                parent_messages = restored_parent.values["messages"]
                general_messages = general_checkpoint.checkpoint["channel_values"][
                    "messages"
                ]
                translation_messages = translation_checkpoint.checkpoint[
                    "channel_values"
                ]["messages"]

                assert restored_parent.values["resolved_capability_id"] == "en_to_zh"
                assert [message.content for message in parent_messages] == [
                    "我喜欢蓝色",
                    "我记住了。",
                    "Good morning.",
                    "早上好。",
                    "It is sunny today.",
                    "今天天气晴朗。",
                ]
                assert [message.content for message in general_messages] == [
                    "我喜欢蓝色",
                    "我记住了。",
                ]
                assert [message.content for message in translation_messages] == [
                    message.content for message in parent_messages
                ]
                assert [
                    message.content
                    for message in restarted_translation_model.captured_messages[-1][1:]
                ] == [message.content for message in parent_messages[:-1]]
                assert route_rejections == [["general_chat"]]
                assert parent_config["configurable"]["thread_id"] == str(session_id)
                assert child_thread_config(session_id, "general_chat") == {
                    "configurable": {
                        "thread_id": f"{session_id}:general_chat",
                    }
                }
                assert child_thread_config(session_id, "en_to_zh") == {
                    "configurable": {
                        "thread_id": f"{session_id}:en_to_zh",
                    }
                }
                assert "draft_translation" not in restored_parent.values
                assert "draft_translation" not in general_checkpoint.checkpoint[
                    "channel_values"
                ]
                assert translation_checkpoint.checkpoint["channel_values"][
                    "draft_translation"
                ] == "今天天气晴朗。"
        finally:
            async with open_stage_one_checkpointers(settings) as cleanup_savers:
                await cleanup_savers.parent.adelete_thread(str(session_id))
                await cleanup_savers.general_chat.adelete_thread(
                    f"{session_id}:general_chat"
                )
                await cleanup_savers.en_to_zh.adelete_thread(f"{session_id}:en_to_zh")

    run_on_psycopg_compatible_loop(exercise())
