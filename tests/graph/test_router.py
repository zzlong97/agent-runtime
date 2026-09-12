import asyncio
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, ValidationError


class FakeStructuredChatModel(BaseChatModel):
    response: dict[str, Any]
    failure_message: str | None = None
    captured_messages: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fake-structured-router"

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
        if self.failure_message is not None:
            raise RuntimeError(self.failure_message)
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "RouterDecision",
                                "args": self.response,
                                "id": "router-call",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )


def test_router_decision_schema_describes_and_validates_every_field() -> None:
    from agent_runtime.graph.router import RouterDecision

    schema = RouterDecision.model_json_schema()

    assert set(schema["properties"]) == {"capability_id", "confidence"}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["capability_id"]["description"] == (
        "路由器从本轮候选列表中选出的唯一能力标识；Stage 1 仅允许 "
        "general_chat 或 en_to_zh。"
    )
    assert schema["properties"]["confidence"]["description"] == (
        "路由器对 capability_id 选择结果的置信度，取值范围为 0.0 至 1.0；"
        "Stage 1 仅记录和校验该值，不触发中断或人工确认。"
    )
    assert schema["properties"]["confidence"]["minimum"] == 0.0
    assert schema["properties"]["confidence"]["maximum"] == 1.0


def test_router_decision_rejects_missing_or_extra_fields() -> None:
    from agent_runtime.graph.router import RouterDecision

    try:
        RouterDecision.model_validate({"capability_id": "general_chat"})
    except ValidationError:
        pass
    else:
        raise AssertionError("缺少 confidence 时应拒绝 Router 输出")

    try:
        RouterDecision.model_validate(
            {
                "capability_id": "general_chat",
                "confidence": 0.8,
                "candidates": [],
            }
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("Stage 1 Router Schema 不应接受 TopN 字段")


def test_router_routes_ordinary_chat_to_general_chat() -> None:
    from agent_runtime.graph.router import ROUTER_SYSTEM_PROMPT, StageOneRouter

    model = FakeStructuredChatModel(
        response={"capability_id": "general_chat", "confidence": 0.9}
    )
    router = StageOneRouter(model)

    result = asyncio.run(
        router.route(
            {
                "messages": [HumanMessage(content="介绍一下 LangGraph")],
                "resolved_capability_id": None,
                "rejected_capability_ids": [],
            },
            {},
        )
    )

    assert result == {"resolved_capability_id": "general_chat"}
    assert "只负责选择能力，不回答用户问题" in ROUTER_SYSTEM_PROMPT
    assert "本轮候选能力" in str(model.captured_messages[-1][-1].content)
    assert "介绍一下 LangGraph" in str(model.captured_messages[-1][-1].content)


def test_router_routes_explicit_english_to_chinese_request() -> None:
    from agent_runtime.graph.router import StageOneRouter

    router = StageOneRouter(
        FakeStructuredChatModel(
            response={"capability_id": "en_to_zh", "confidence": 0.97}
        )
    )

    result = asyncio.run(
        router.route(
            {
                "messages": [
                    HumanMessage(content='Translate "How are you?" into Chinese')
                ],
                "resolved_capability_id": None,
                "rejected_capability_ids": [],
            },
            {},
        )
    )

    assert result == {"resolved_capability_id": "en_to_zh"}


def test_router_does_not_interrupt_on_low_confidence() -> None:
    from agent_runtime.graph.router import StageOneRouter

    router = StageOneRouter(
        FakeStructuredChatModel(
            response={"capability_id": "general_chat", "confidence": 0.01}
        )
    )

    result = asyncio.run(
        router.route(
            {
                "messages": [HumanMessage(content="这个问题可能有点模糊")],
                "resolved_capability_id": None,
                "rejected_capability_ids": [],
            },
            {},
        )
    )

    assert result == {"resolved_capability_id": "general_chat"}


def test_router_excludes_rejected_capability_from_model_candidates() -> None:
    from agent_runtime.graph.router import StageOneRouter

    model = FakeStructuredChatModel(
        response={"capability_id": "en_to_zh", "confidence": 0.95}
    )
    router = StageOneRouter(model)

    result = asyncio.run(
        router.route(
            {
                "messages": [
                    HumanMessage(content='Translate "How are you?" into Chinese')
                ],
                "resolved_capability_id": None,
                "rejected_capability_ids": ["general_chat"],
            },
            {},
        )
    )

    prompt = str(model.captured_messages[-1][-1].content)
    assert result == {"resolved_capability_id": "en_to_zh"}
    assert "- en_to_zh:" in prompt
    assert "general_chat" not in prompt


def test_router_rejects_model_choice_outside_current_candidates() -> None:
    from agent_runtime.graph.router import RouterError, StageOneRouter

    router = StageOneRouter(
        FakeStructuredChatModel(
            response={"capability_id": "general_chat", "confidence": 0.99}
        )
    )

    with pytest.raises(RouterError) as captured:
        asyncio.run(
            router.route(
                {
                    "messages": [HumanMessage(content="Good morning")],
                    "resolved_capability_id": None,
                    "rejected_capability_ids": ["general_chat"],
                },
                {},
            )
        )

    assert captured.value.code == "ROUTER_INVALID_CAPABILITY"
    assert captured.value.message == "路由模型选择了本轮候选列表之外的能力"
    assert not hasattr(captured.value, "control_signal")


def test_router_converts_schema_validation_failure_to_runtime_error() -> None:
    from agent_runtime.graph.router import RouterError, StageOneRouter

    router = StageOneRouter(
        FakeStructuredChatModel(response={"capability_id": "general_chat"})
    )

    with pytest.raises(RouterError) as captured:
        asyncio.run(
            router.route(
                {
                    "messages": [HumanMessage(content="你好")],
                    "resolved_capability_id": None,
                    "rejected_capability_ids": [],
                },
                {},
            )
        )

    assert captured.value.code == "ROUTER_INVALID_OUTPUT"
    assert captured.value.message == "路由模型返回的结构化结果不符合 Schema"
    assert isinstance(captured.value.__cause__, ValidationError)
    assert not hasattr(captured.value, "control_signal")


def test_router_model_failure_is_runtime_error_not_out_of_scope() -> None:
    from agent_runtime.graph.router import RouterError, StageOneRouter

    router = StageOneRouter(
        FakeStructuredChatModel(
            response={"capability_id": "general_chat", "confidence": 0.8},
            failure_message="provider unavailable",
        )
    )

    with pytest.raises(RouterError) as captured:
        asyncio.run(
            router.route(
                {
                    "messages": [HumanMessage(content="你好")],
                    "resolved_capability_id": None,
                    "rejected_capability_ids": [],
                },
                {},
            )
        )

    assert captured.value.code == "ROUTER_CALL_FAILED"
    assert captured.value.message == "路由模型调用失败"
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert not hasattr(captured.value, "control_signal")


def test_router_rejects_request_when_no_candidate_remains() -> None:
    from agent_runtime.graph.router import RouterError, StageOneRouter

    model = FakeStructuredChatModel(
        response={"capability_id": "general_chat", "confidence": 0.8}
    )
    router = StageOneRouter(model)

    with pytest.raises(RouterError) as captured:
        asyncio.run(
            router.route(
                {
                    "messages": [HumanMessage(content="继续")],
                    "resolved_capability_id": None,
                    "rejected_capability_ids": ["general_chat", "en_to_zh"],
                },
                {},
            )
        )

    assert captured.value.code == "ROUTER_NO_CANDIDATE"
    assert captured.value.message == "本轮没有可供路由的能力"
    assert model.captured_messages == []


def test_router_rejects_parent_state_without_human_message() -> None:
    from agent_runtime.graph.router import RouterError, StageOneRouter

    model = FakeStructuredChatModel(
        response={"capability_id": "general_chat", "confidence": 0.8}
    )
    router = StageOneRouter(model)

    with pytest.raises(RouterError) as captured:
        asyncio.run(
            router.route(
                {
                    "messages": [AIMessage(content="上一条回复")],
                    "resolved_capability_id": None,
                    "rejected_capability_ids": [],
                },
                {},
            )
        )

    assert captured.value.code == "ROUTER_INVALID_INPUT"
    assert captured.value.message == "Parent State 中缺少可路由的 HumanMessage"
    assert model.captured_messages == []


def test_router_is_usable_as_parent_graph_route_node() -> None:
    from agent_runtime.graph.child_result import CapabilityInvocation, ChildResult
    from agent_runtime.graph.parent import build_parent_graph
    from agent_runtime.graph.router import StageOneRouter

    invoked_capabilities: list[str | None] = []
    router = StageOneRouter(
        FakeStructuredChatModel(
            response={"capability_id": "en_to_zh", "confidence": 0.92}
        )
    )

    async def invoke_capability(state, config):
        invoked_capabilities.append(state["resolved_capability_id"])
        return CapabilityInvocation(
            result=ChildResult(status="completed", control_signal=None),
            message=AIMessage(content="你好", id="router-parent-message"),
        )

    parent = build_parent_graph(
        route=router.route,
        invoke_capability=invoke_capability,
    )

    asyncio.run(
        parent.ainvoke(
            {
                "messages": [HumanMessage(content="Translate hello into Chinese")],
                "resolved_capability_id": None,
                "rejected_capability_ids": [],
            }
        )
    )

    assert invoked_capabilities == ["en_to_zh"]
