"""使用真实 OpenAI SDK 与隔离 HTTP 端点验证结构化输出协议。"""

import asyncio
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

from agent_runtime.capabilities.en_to_zh.graph import EnglishToChineseCapability
from agent_runtime.capabilities.general_chat.agent import GeneralChatCapability
from agent_runtime.graph.router import RouterError, StageOneRouter
from agent_runtime.runtime.agent_contract import (
    AgentCancellation,
    AgentContext,
    AgentEventOutlet,
    RunContext,
    TaskInput,
)
from tests.graph.test_router import FakeCandidateProvider


@asynccontextmanager
async def compatible_model(decisions):
    """模拟不支持 json_schema、支持工具调用的端点，不替换 SDK 或解析器。"""

    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload.get("tools"):
            name = payload["tools"][0]["function"]["name"]
            arguments = json.dumps(decisions[name])
            midpoint = len(arguments) // 2
            deltas = [
                {"role": "assistant", "tool_calls": [{
                    "index": 0, "id": "call-structured", "type": "function",
                    "function": {"name": name, "arguments": arguments[:midpoint]},
                }]},
                {"tool_calls": [{
                    "index": 0, "function": {"arguments": arguments[midpoint:]},
                }]},
            ]
            finish_reason = "tool_calls"
        else:
            # 复现兼容端点忽略 response_format 后自行猜测字段的真实故障。
            content = (
                '{"capability":"general_chat","task_id":null}'
                if payload.get("response_format")
                else "验证通过"
            )
            deltas = [{"role": "assistant", "content": content}]
            finish_reason = "stop"
        chunks = [
            {"id": "chatcmpl-contract", "object": "chat.completion.chunk",
             "created": 1, "model": "compatible-test",
             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
            for delta in deltas
        ]
        chunks.append({
            "id": "chatcmpl-contract", "object": "chat.completion.chunk",
            "created": 1, "model": "compatible-test",
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        })
        stream = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=stream + "data: [DONE]\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        yield ChatOpenAI(
            api_key="test-secret", base_url="https://compatible.invalid/v1",
            model="compatible-test", streaming=True, max_retries=0,
            http_async_client=client,
        ), requests


def assert_structured_request(request, name, fields):
    """确认模型实际收到关闭 Schema，而非只有提示词或 JSON Object 约束。"""

    assert "response_format" not in request
    assert request["tool_choice"] == {
        "type": "function", "function": {"name": name},
    }
    assert request["parallel_tool_calls"] is False
    assert len(request["tools"]) == 1
    parameters = request["tools"][0]["function"]["parameters"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == set(parameters["required"]) == fields
    assert all(field["description"] for field in parameters["properties"].values())


@pytest.mark.parametrize(("decision", "error_code"), [
    ({"capability_id": "general_chat", "task_action": "new", "confidence": 0.9}, None),
    ({"capability_id": "general_chat", "task_action": "new"}, "ROUTER_INVALID_OUTPUT"),
    ({"capability_id": "general_chat", "task_action": "new", "confidence": 0.9,
      "task_id": "不得透传"}, "ROUTER_INVALID_OUTPUT"),
    ({"capability_id": "unknown_capability", "task_action": "new", "confidence": 0.9},
     "ROUTER_INVALID_CAPABILITY"),
])
def test_router_uses_compatible_protocol_and_keeps_strict_validation(
    decision, error_code,
):
    async def scenario():
        async with compatible_model({"RouterDecision": decision}) as (model, requests):
            router = StageOneRouter(
                model, candidate_provider=FakeCandidateProvider(), user_id="runtime-user",
            )
            state = {
                "messages": [HumanMessage(content="你好")],
                "resolved_capability_id": None, "rejected_capability_ids": [],
            }
            if error_code:
                with pytest.raises(RouterError) as caught:
                    await router.route(state, {})
                assert caught.value.code == error_code
            else:
                assert await router.route(state, {}) == {
                    "resolved_capability_id": "general_chat", "task_action": "new",
                }
            assert len(requests) == 1
            assert_structured_request(
                requests[0], "RouterDecision",
                {"capability_id", "task_action", "confidence"},
            )
    asyncio.run(scenario())


@pytest.mark.parametrize("capability_id", ["general_chat", "en_to_zh"])
@pytest.mark.parametrize("accepted", [True, False])
def test_scope_guards_use_compatible_protocol_before_visible_generation(
    capability_id, accepted,
):
    async def never_cancelled():
        return False

    async def scenario():
        if capability_id == "general_chat":
            name, field = "GeneralChatScopeDecision", "is_translation_request"
            decision = {field: not accepted}
        else:
            name, field = "EnglishToChineseScopeDecision", "can_translate_to_chinese"
            decision = {field: accepted}
        async with compatible_model({name: decision}) as (model, requests):
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
                task_input=TaskInput(messages=(HumanMessage(content="合成测试输入"),)),
            )
            assert_structured_request(requests[0], name, {field})
            if accepted:
                assert result.status == "completed"
                assert result.content == "验证通过"
                assert "".join(event.text for event in events) == "验证通过"
                assert len(requests) == 2
            else:
                assert result.status == "rejected"
                assert result.metadata.control_signal == "OUT_OF_SCOPE"
                assert events == [] and result.content == ""
                assert saver.get_tuple(config) is None
                assert len(requests) == 1
    asyncio.run(scenario())
